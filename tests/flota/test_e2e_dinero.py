"""
El ciclo del dinero de punta a punta — las cuatro fases sobre UN vehículo.

Las cuatro fases (gasto+tanqueo, taller+garantía, preventivo, llantas) las
escribieron cuatro agentes distintos que nunca se vieron. **Cada una se probó
sola.** Este archivo prueba lo otro: que el dinero atraviese las cuatro y que
los cruces ENTRE fases funcionen.

## Qué prueba y qué NO prueba

QUÉ AFIRMA: que un mes realista de un vehículo —tanqueos, una OT con su
factura, un juego de llantas, el SOAT prorrateado y las lecturas de odómetro—
produce el CPK que sale de calcularlo a mano, y que cada extremidad de
`flota_gasto` aporta sus pesos.

QUÉ NO AFIRMA: nada sobre magnitudes reales. Los insumos son sintéticos y
**elegidos para que cada división sea exacta**, igual que el §5 del canon. No
hay un peso de flota registrado en producción.

## La forma que se verifica

    flota_gasto  ← la espina
       ├── flota_tanqueo        (fase 1)
       ├── flota_intervencion   (fase 2)
       └── flota_montaje_llanta (fase 4)

El plan dice: *«si cada tabla lleva su propia columna de valor, el CPK es un
UNION de N ramas y alguien va a olvidar la N+1»*. `cpk_de` suma **la espina** y
no hace ningún UNION, así que la propiedad que hay que probar no es que sume
tres ramas: es que **las tres extremidades escriban en la espina**. Un montaje
de llanta que no atara su gasto, o una factura de taller que escribiera su
propio valor, se verían exactamente igual desde el CPK: plata que se pagó y no
aparece.

**Ese es el resultado principal y es bueno:** el CPK de abril da exactamente lo
que sale de calcularlo a mano, con las tres extremidades cargadas. La espina
aguanta.

## Los cuatro `xfail(strict=True)` de este archivo

Cada uno es un defecto **medido y registrado, no escondido** — el día que
alguien lo arregle, el `strict` avisa. Ninguno se afloja ni se borra.

| Dónde | Qué | Cómo falla |
|---|---|---|
| `preventivo.diagnostico_de` | `Diagnostico.marca_ritmo` se calcula y no se publica: cero consumidores en el repo | silencio |
| `llantas._validar_gasto` | un montaje se cuelga de un gasto de `soat` sin chistar | silencio |
| `flota.js::flotaMontarLlanta` | el POST de montaje nunca manda `gasto_id`; el NULL se pinta como «rotación» | silencio, y afirmando de más |
| `hallazgos.cerrar` | cierra un daño sin OT, sin evidencia y sin nota — contra la regla 6 del módulo | silencio, con 200 |

Y dos cosas más quedan **medidas sin `xfail`**, porque cerrarlas es una
decisión de diseño y no un arreglo: el taller y el preventivo no se hablan
(`TestElTallerYElPreventivoNoSeHablan`) y la factura del taller cae en el mes
de su fecha, no en el del trabajo (`TestLaFacturaDelTallerCaeEnElMesDeSuFECHA`).
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from flota.adaptadores import gastos as adaptador_gastos
from flota.adaptadores import llantas as adaptador_llantas
from flota.adaptadores import preventivo as adaptador_prev
from flota.adaptadores import taller as adaptador_taller
from flota.dominio.costos import SIN_DATO


def _paso_por_la_cola(vehiculo_id, usuario_id):
    """Verifica las lecturas dudosas del vehículo, como haría una persona.

    Misma función que `tests/flota/test_gastos.py::_alguien_paso_por_la_cola`, y
    **se repite a propósito en vez de importarse**: importar un helper de otro
    archivo de tests ata dos suites que corren en paralelo y que otro agente
    puede estar editando. Lo que no se repite es ninguna política — esto solo
    llama a `verificacion.verificar`, que es la única implementación.

    Sin esta llamada el mes entero sale `sin_dato`: ninguna lectura de un
    tanqueo, de una OT ni de un montaje tiene foto del tablero, así que todas
    nacen `dudosa` (regla 1) y `confianza_del_tramo` devuelve `SIN_DATO`.
    """
    from flota.adaptadores import verificacion

    for lectura, _placa in verificacion.pendientes():
        if lectura.vehiculo_id == vehiculo_id:
            verificacion.verificar(lectura_id=lectura.id, usuario_id=usuario_id)


@pytest.fixture
def flota(db, almacen):
    """Un NHR con ficha completa y un usuario de control de flota."""
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import FichaTecnica

    veh = Vehiculo(placa='E2E100', tipo='NHR', activo=True)
    jefe = Usuario(nombre='Control E2E', email='e2e_flota@test.com',
                   password_hash=generate_password_hash('x'),
                   rol='control_flota', almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, jefe])
    db.session.flush()
    db.session.add(FichaTecnica(
        vehiculo_id=veh.id, posiciones_llanta=6, km_inicial=199000,
        km_inicial_ts=datetime(2026, 1, 1), combustible='diesel',
        capacidad_tanque_galones=Decimal('40'),
        capacidad_tanque_fuente='manual_fabricante',
        distribucion_km_cambio=60000,
        distribucion_fuente='manual_fabricante'))
    db.session.commit()
    return {'vehiculo_id': veh.id, 'placa': veh.placa, 'usuario_id': jefe.id}


# ══════════════════════════════════════════════════════════════════════════
# EL MES COMPLETO — abril de 2026, con las tres extremidades cargadas
# ══════════════════════════════════════════════════════════════════════════
#
# Insumos sintéticos y declarados como tales, elegidos para que cada división
# sea exacta (igual que el canon §5). El cálculo a mano:
#
#   SOAT      730.000 × 30 ÷ 365                          =    60.000
#   combustible 168.000 + 280.000 + 420.000               =   868.000
#   taller (mantenimiento, factura de la OT)              =   350.000
#   llantas (compra del juego, categoría `llanta`)        = 1.200.000
#                                                           ──────────
#   pesos imputados a abril                                 2.478.000
#   kilómetros de abril     202.000 − 200.000              =     2.000
#   CPK                     2.478.000 ÷ 2.000              =     1.239
#
# Verificación de orden de magnitud: solo el combustible son $434/km, el 35%
# del total. A 28 km/galón con el galón a $14.000 el combustible TIENE que dar
# $500/km sobre los kilómetros que ese combustible movió — y no los movió
# todos: los 12 galones del primer lleno se quemaron en marzo. Ver el §5 del
# canon, mismo razonamiento.

CPK_ESPERADO = Decimal('1239')
PESOS_ESPERADOS = Decimal('2478000')
KM_ESPERADOS = 2000

ABRIL_DESDE = date(2026, 4, 1)
ABRIL_HASTA = date(2026, 4, 30)


@pytest.fixture
def abril(app, db, flota):
    """El mes completo, cargado por los adaptadores reales de las cuatro fases.

    **Ninguna fila se inserta a mano** salvo la lectura de cierre de mes, que es
    un gesto real (`origen='cierre_dia'`) y no tiene adaptador propio en esta
    fase. Un arnés que escribiera `Gasto(...)` directo solo probaría que la base
    acepta esas filas.

    El orden es cronológico y **no es cosmético**: la monotonía del odómetro
    mira el máximo ya registrado, así que un evento cargado fuera de orden con
    un kilometraje menor no entra.
    """
    from flota.adaptadores.modelos import LecturaOdometro

    v = flota['vehiculo_id']
    u = flota['usuario_id']

    def tanquear(km, galones, valor, dia, ts):
        return adaptador_gastos.registrar_tanqueo(
            vehiculo_id=v, fecha=dia, valor=valor, galones=galones,
            tanque='lleno', estacion='Terpel Neiva', km=km, proveedor='Terpel',
            origen_costo='tarjeta_convenio', registrado_por_usuario_id=u, ts=ts)

    # ── El SOAT anual: de escritorio, se cuelga de la última lectura ─────
    # Va primero porque necesita que YA exista una lectura; por eso el mes
    # arranca con una lectura de apertura real.
    db.session.add(LecturaOdometro(
        vehiculo_id=v, valor_km=200000, ts=datetime(2026, 4, 1, 11, 0),
        origen='cierre_dia', autor_usuario_id=u))
    db.session.commit()
    adaptador_gastos.registrar_gasto(
        vehiculo_id=v, categoria='soat', fecha=date(2026, 1, 1),
        valor='730000', proveedor='Seguros del Estado',
        origen_costo='credito_proveedor', registrado_por_usuario_id=u,
        periodo_desde=date(2026, 1, 1), periodo_hasta=date(2026, 12, 31),
        ts=datetime(2026, 4, 1, 12, 0))

    # ── Fase 1 · tres tanqueos llenos ────────────────────────────────────
    tanquear(200000, '12', '168000', date(2026, 4, 5), datetime(2026, 4, 5, 13, 0))
    tanquear(200600, '20', '280000', date(2026, 4, 15), datetime(2026, 4, 15, 13, 0))

    # ── Fase 2 · el taller: OT, intervención con garantía, y su factura ──
    orden = adaptador_taller.abrir(
        vehiculo_id=v, tipo='correctiva', taller='Serviteca del Sur',
        descripcion='Pastillas delanteras chirriando', km=200900,
        abierta_por_usuario_id=u, ts=datetime(2026, 4, 18, 13, 0))
    interv = adaptador_taller.registrar_intervencion(
        orden_trabajo_id=orden.id, sistema='frenos', garantia_declarada='si',
        garantia_meses=6, garantia_km=20000, registrada_por_usuario_id=u,
        ts=datetime(2026, 4, 18, 14, 0))
    adaptador_taller.cerrar(orden_trabajo_id=orden.id, usuario_id=u,
                            ts=datetime(2026, 4, 18, 15, 0))
    gasto_taller = adaptador_taller.registrar_factura(
        orden_trabajo_id=orden.id, intervencion_ids=[interv.id],
        categoria='mantenimiento', fecha=date(2026, 4, 20), valor='350000',
        proveedor='Serviteca del Sur', origen_costo='credito_proveedor',
        registrado_por_usuario_id=u, documento_numero='FV-8891',
        ts=datetime(2026, 4, 20, 13, 0))

    # ── Fase 4 · la compra del juego y sus seis montajes ─────────────────
    # La compra es un `flota_gasto` con `categoria='llanta'`, que es de campo:
    # lleva su propio kilometraje. Los seis montajes se atan a ESE gasto.
    gasto_llantas = adaptador_gastos.registrar_gasto(
        vehiculo_id=v, categoria='llanta', fecha=date(2026, 4, 22),
        valor='1200000', proveedor='Llantas del Huila',
        origen_costo='credito_proveedor', km=201200,
        registrado_por_usuario_id=u, documento_numero='FV-4412',
        ts=datetime(2026, 4, 22, 13, 0))
    montajes = []
    for pos in range(1, 7):
        ll = adaptador_llantas.dar_de_alta(
            codigo=f'E2E-LL-{pos}', medida='215/75R17.5',
            registrada_por_usuario_id=u, ts=datetime(2026, 4, 22, 13, 0))
        montajes.append(adaptador_llantas.montar(
            llanta_id=ll.id, vehiculo_id=v, posicion=pos, km=201200,
            montada_por_usuario_id=u, gasto_id=gasto_llantas.id,
            ts=datetime(2026, 4, 22, 14, 0)))

    tanquear(201400, '30', '420000', date(2026, 4, 25), datetime(2026, 4, 25, 13, 0))

    # ── El cierre del mes ────────────────────────────────────────────────
    db.session.add(LecturaOdometro(
        vehiculo_id=v, valor_km=202000, ts=datetime(2026, 4, 30, 18, 0),
        origen='cierre_dia', autor_usuario_id=u))
    db.session.commit()

    _paso_por_la_cola(v, u)
    return dict(flota, orden_id=orden.id, intervencion_id=interv.id,
                gasto_taller_id=gasto_taller.id,
                gasto_llantas_id=gasto_llantas.id,
                montaje_ids=[m.id for m in montajes])


class TestElCPKConTodoCargado:
    """El número calculado a mano contra el que devuelve el sistema."""

    def test_el_CPK_de_abril_es_1239(self, app, db, abril):
        r = adaptador_gastos.cpk_de(abril['vehiculo_id'],
                                    ABRIL_DESDE, ABRIL_HASTA)
        assert r['pesos'] == PESOS_ESPERADOS
        assert r['km'] == KM_ESPERADOS
        assert r['cpk'] == CPK_ESPERADO
        assert r['marca'] == 'verificada'

    def test_las_cuatro_fases_aportan_pesos_y_ninguna_falta(self, app, db, abril):
        """La descomposición por categoría, sumada aparte y cuadrada contra el
        total. **Es la comprobación de la N+1**: si una extremidad dejara de
        escribir en la espina, su renglón valdría cero acá y el total no
        cuadraría — en vez de que el CPK bajara sin que nada se vea raro.
        """
        from flota.adaptadores.modelos import Gasto
        from flota.dominio import costos

        por_categoria = {}
        for g in Gasto.query.filter_by(vehiculo_id=abril['vehiculo_id']).all():
            por_categoria[g.categoria] = por_categoria.get(
                g.categoria, Decimal('0')) + costos.imputar_a_ventana(
                    valor=Decimal(g.valor), periodo_desde=g.periodo_desde,
                    periodo_hasta=g.periodo_hasta,
                    ventana_desde=ABRIL_DESDE, ventana_hasta=ABRIL_HASTA)

        assert por_categoria == {
            'soat': Decimal('60000'),            # fase 1 · gasto de escritorio
            'combustible': Decimal('868000'),    # fase 1 · flota_tanqueo
            'mantenimiento': Decimal('350000'),  # fase 2 · flota_intervencion
            'llanta': Decimal('1200000'),        # fase 4 · flota_montaje_llanta
        }
        assert sum(por_categoria.values(), Decimal('0')) == PESOS_ESPERADOS
        assert adaptador_gastos.cpk_de(
            abril['vehiculo_id'], ABRIL_DESDE, ABRIL_HASTA
        )['pesos'] == PESOS_ESPERADOS

    def test_el_taller_esta_adentro(self, app, db, abril):
        """Sin la factura del taller, abril daría $1.064 por kilómetro.

        Es la comprobación que separa un CPK completo de uno que suma solo la
        rama que su autor tenía a la vista.
        """
        r = adaptador_gastos.cpk_de(abril['vehiculo_id'],
                                    ABRIL_DESDE, ABRIL_HASTA)
        assert r['pesos'] - Decimal('350000') == Decimal('2128000')
        assert r['cpk'] != Decimal('1064')

    def test_las_llantas_estan_adentro(self, app, db, abril):
        """Sin la compra del juego, abril daría $639 por kilómetro — casi la
        mitad. La llanta es la extremidad más cara y la más fácil de olvidar:
        su tabla propia (`flota_montaje_llanta`) no lleva valor."""
        r = adaptador_gastos.cpk_de(abril['vehiculo_id'],
                                    ABRIL_DESDE, ABRIL_HASTA)
        assert r['pesos'] - Decimal('1200000') == Decimal('1278000')
        assert r['cpk'] != Decimal('639')

    def test_el_SOAT_entra_repartido_no_entero(self, app, db, abril):
        """Con el SOAT entero abril daría $1.574 por kilómetro."""
        r = adaptador_gastos.cpk_de(abril['vehiculo_id'],
                                    ABRIL_DESDE, ABRIL_HASTA)
        assert r['pesos'] == PESOS_ESPERADOS
        assert r['pesos'] != Decimal('730000') + Decimal('2418000')

    def test_ninguna_extremidad_duplica_un_peso(self, app, db, abril):
        """Seis montajes apuntando al mismo gasto **no suman seis veces**.

        Es la propiedad que hace que la espina sirva: el CPK suma
        `flota_gasto`, no las filas que lo referencian. Si alguien reescribiera
        `cpk_de` como un `UNION` sobre las extremidades, este test se cae.
        """
        from flota.adaptadores.modelos import Gasto, MontajeLlanta

        montajes = MontajeLlanta.query.filter_by(
            gasto_id=abril['gasto_llantas_id']).all()
        assert len(montajes) == 6
        gastos = Gasto.query.filter_by(vehiculo_id=abril['vehiculo_id']).all()
        assert sum(Decimal(g.valor) for g in gastos) == Decimal('3148000')

        r = adaptador_gastos.cpk_de(abril['vehiculo_id'],
                                    ABRIL_DESDE, ABRIL_HASTA)
        assert r['pesos'] == PESOS_ESPERADOS

    def test_el_rendimiento_del_mes_es_28(self, app, db, abril):
        """Agregado sumando km y galones, no promediando razones: 28,0 y no
        28,33. Y los 12 galones del primer lleno no entran en ninguna ventana:
        se quemaron antes, en un tramo que nadie midió."""
        assert adaptador_gastos.rendimiento_de(abril['vehiculo_id']) == Decimal('28')

    def test_sin_pasar_por_la_cola_el_mes_entero_es_sin_dato(self, app, db, flota):
        """El otro lado del mismo hecho: sin verificación, no hay CPK.

        Se rearma un mes mínimo sin pasar por la cola. Las dos lecturas nacen
        `dudosa` —el formulario de tanqueo no pide foto del tablero— y
        `confianza_del_tramo` devuelve `SIN_DATO`. **No es un CPK bajo: no es un
        CPK.**
        """
        v, u = flota['vehiculo_id'], flota['usuario_id']
        for km, ts in ((200000, datetime(2026, 4, 5, 13, 0)),
                       (200600, datetime(2026, 4, 15, 13, 0))):
            adaptador_gastos.registrar_tanqueo(
                vehiculo_id=v, fecha=ts.date(), valor='168000', galones='12',
                tanque='lleno', estacion='Terpel', km=km, proveedor='Terpel',
                origen_costo='tarjeta_convenio',
                registrado_por_usuario_id=u, ts=ts)

        r = adaptador_gastos.cpk_de(v, ABRIL_DESDE, ABRIL_HASTA)
        assert r['cpk'] == SIN_DATO
        assert r['marca'] == SIN_DATO


# ══════════════════════════════════════════════════════════════════════════
# CRUCE fase 2 × fase 2 — la garantía, por las DOS vías
# ══════════════════════════════════════════════════════════════════════════

class TestLaGarantiaSeVeAntesDeMandarElCamion:
    """*«El sistema busca intervenciones con garantía vigente —por fecha O por
    km— y lo muestra antes de mandar el camión»*. Las dos vías, y el detector
    también en la dirección que NO debe disparar."""

    def test_la_garantia_del_taller_aparece_sobre_el_mismo_sistema(
            self, app, db, abril):
        vigentes = adaptador_taller.garantias_vigentes_de(
            abril['vehiculo_id'], date(2026, 5, 10), sistema='frenos')
        assert [g['intervencion_id'] for g in vigentes] == [abril['intervencion_id']]
        assert vigentes[0]['por_fecha'] is True
        assert vigentes[0]['por_km'] is True

    def test_no_dispara_sobre_OTRO_sistema(self, app, db, abril):
        """El detector en la dirección que no debe disparar. Un motor no está
        cubierto por la garantía de unos frenos, y mostrarlo enseñaría a
        ignorar el renglón."""
        assert adaptador_taller.garantias_vigentes_de(
            abril['vehiculo_id'], date(2026, 5, 10), sistema='motor') == []

    def test_cubre_por_KM_aunque_la_fecha_ya_venciera(self, app, db, abril):
        """La primera de las dos vías, aislada.

        Seis meses desde el 18 de abril vencen el 18 de octubre; el 19 de
        octubre `por_fecha` es `False`. El odómetro sigue en 202.000 contra un
        tope de 220.900, así que `por_km` cubre — y **basta una**.
        """
        vigentes = adaptador_taller.garantias_vigentes_de(
            abril['vehiculo_id'], date(2026, 10, 19), sistema='frenos')
        assert len(vigentes) == 1
        assert vigentes[0]['por_fecha'] is False
        assert vigentes[0]['por_km'] is True

    def test_cubre_por_FECHA_aunque_el_km_ya_se_pasara(self, app, db, flota):
        """La segunda vía, aislada: garantía solo por kilómetros ya excedidos
        contra una que solo tiene meses.

        Se arma aparte porque exige mover el odómetro más allá del tope, y eso
        sobre el mes de abril rompería el CPK que el otro bloque mide.
        """
        v, u = flota['vehiculo_id'], flota['usuario_id']
        orden = adaptador_taller.abrir(
            vehiculo_id=v, tipo='correctiva', taller='Serviteca',
            descripcion='Embrague patinando', km=200000,
            abierta_por_usuario_id=u, ts=datetime(2026, 4, 1, 13, 0))
        interv = adaptador_taller.registrar_intervencion(
            orden_trabajo_id=orden.id, sistema='embrague',
            garantia_declarada='si', garantia_meses=6, garantia_km=1000,
            registrada_por_usuario_id=u, ts=datetime(2026, 4, 1, 14, 0))
        # El camión rueda 5.000 km: el tope por km (201.000) queda atrás.
        from flota.adaptadores.modelos import LecturaOdometro
        db.session.add(LecturaOdometro(
            vehiculo_id=v, valor_km=205000, ts=datetime(2026, 5, 2, 13, 0),
            origen='cierre_dia', autor_usuario_id=u))
        db.session.commit()

        vigentes = adaptador_taller.garantias_vigentes_de(
            v, date(2026, 5, 2), sistema='embrague')
        assert [g['intervencion_id'] for g in vigentes] == [interv.id]
        assert vigentes[0]['por_km'] is False
        assert vigentes[0]['por_fecha'] is True

    def test_no_dispara_sobre_una_garantia_vencida_por_las_DOS_vias(
            self, app, db, abril):
        """El día en que ninguna de las dos dimensiones cubre, el renglón
        desaparece. Sin esto, «garantía vigente» sería un adorno permanente."""
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.add(LecturaOdometro(
            vehiculo_id=abril['vehiculo_id'], valor_km=250000,
            ts=datetime(2026, 11, 1, 13, 0), origen='cierre_dia',
            autor_usuario_id=abril['usuario_id']))
        db.session.commit()

        assert adaptador_taller.garantias_vigentes_de(
            abril['vehiculo_id'], date(2026, 11, 1), sistema='frenos') == []

    def test_una_orden_anulada_no_deja_garantia(self, app, db, flota):
        """Una visita que no ocurrió no cubre nada. Es la vía barata de la
        regla 11 si no se cierra: abrir, declarar garantía, anular, y quedarse
        con el renglón verde."""
        v, u = flota['vehiculo_id'], flota['usuario_id']
        orden = adaptador_taller.abrir(
            vehiculo_id=v, tipo='correctiva', taller='Serviteca',
            descripcion='Revisión', km=200000, abierta_por_usuario_id=u,
            ts=datetime(2026, 4, 1, 13, 0))
        adaptador_taller.anular(orden_trabajo_id=orden.id, usuario_id=u,
                                motivo='El camión nunca entró')
        assert adaptador_taller.garantias_vigentes_de(
            v, date(2026, 5, 1)) == []


# ══════════════════════════════════════════════════════════════════════════
# CRUCE fase 4 × fase 1 — la llanta y el gasto que la compró
# ══════════════════════════════════════════════════════════════════════════

class TestLaLlantaSeAtaAlGastoQueLaCompro:

    def test_el_montaje_apunta_al_gasto_de_la_compra(self, app, db, abril):
        from flota.adaptadores.modelos import Gasto, MontajeLlanta

        m = MontajeLlanta.query.get(abril['montaje_ids'][0])
        assert m.gasto_id == abril['gasto_llantas_id']
        assert Gasto.query.get(m.gasto_id).categoria == 'llanta'

    def test_un_montaje_no_se_cuelga_del_gasto_de_otro_camion(
            self, app, db, abril, almacen):
        """La única pregunta que esta referencia contesta es *«¿de qué factura
        salió esta llanta?»*, y apuntar a un documento real que no es el suyo
        se lee con confianza y se lee mal."""
        from app.models.vehiculo import Vehiculo

        otro = Vehiculo(placa='E2E200', tipo='NHR', activo=True)
        db.session.add(otro)
        db.session.commit()
        ll = adaptador_llantas.dar_de_alta(
            codigo='E2E-LL-AJENA', medida='215/75R17.5',
            registrada_por_usuario_id=abril['usuario_id'])
        with pytest.raises(adaptador_llantas.LlantaInvalida):
            adaptador_llantas.montar(
                llanta_id=ll.id, vehiculo_id=otro.id, posicion=1, km=1000,
                montada_por_usuario_id=abril['usuario_id'],
                gasto_id=abril['gasto_llantas_id'])

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO. `llantas._validar_gasto` comprueba que el gasto exista y que '
        'sea del MISMO VEHÍCULO, y no comprueba nada más: un montaje se puede '
        'colgar de un gasto de `soat`. Su propio docstring dice para qué existe '
        'la referencia —«¿de qué factura salió esta llanta?»— y advierte que '
        'apuntar «a un documento real que no es el suyo» hace que «la respuesta '
        'se lea con confianza y se lea mal». Un SOAT es exactamente eso: real, '
        'del vehículo correcto, y jamás la factura de una llanta. '
        'Falla EN SILENCIO: no rompe ninguna FK, el CPK sigue saliendo bien '
        '—el gasto está en la espina y donde tiene que estar— y ninguna '
        'pantalla se ve rara. El conjunto que sí puede facturar una llanta ya '
        'está declarado en el repo: `taller.CATEGORIAS_DE_TALLER` '
        '(mantenimiento, repuesto, llanta) — una factura de taller que cubre '
        'mano de obra y llantas es un caso real, así que el arreglo NO es '
        'restringir a `llanta` sola. Elegir el conjunto es una decisión, y por '
        'eso queda registrado en vez de arreglado a ojo.'))
    def test_un_montaje_no_se_cuelga_de_la_poliza_del_SOAT(
            self, app, db, flota):
        """Se arma sobre el vehículo limpio y con la posición 1 libre, para que
        lo único que pueda rechazar el montaje sea la categoría del gasto."""
        from flota.adaptadores.modelos import LecturaOdometro

        v, u = flota['vehiculo_id'], flota['usuario_id']
        db.session.add(LecturaOdometro(
            vehiculo_id=v, valor_km=200000, ts=datetime(2026, 4, 1, 12, 0),
            origen='cierre_dia', autor_usuario_id=u))
        db.session.commit()
        soat = adaptador_gastos.registrar_gasto(
            vehiculo_id=v, categoria='soat', fecha=date(2026, 1, 1),
            valor='730000', proveedor='Seguros del Estado',
            origen_costo='credito_proveedor', registrado_por_usuario_id=u,
            periodo_desde=date(2026, 1, 1), periodo_hasta=date(2026, 12, 31),
            ts=datetime(2026, 4, 1, 13, 0))
        ll = adaptador_llantas.dar_de_alta(
            codigo='E2E-LL-SOAT', medida='215/75R17.5',
            registrada_por_usuario_id=u)
        with pytest.raises(adaptador_llantas.LlantaInvalida):
            adaptador_llantas.montar(
                llanta_id=ll.id, vehiculo_id=v, posicion=1, km=200000,
                montada_por_usuario_id=u, gasto_id=soat.id,
                ts=datetime(2026, 4, 2, 13, 0))

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO — el parámetro existe, se valida, y NADIE lo manda. '
        '`llantas.montar` recibe `gasto_id`, `POST /flota/montajes` lo lee del '
        'cuerpo y `_validar_gasto` lo comprueba… y `flotaMontarLlanta()` en '
        '`app/static/pwa/flota.js` arma su body con `placa`, `llanta_id`, '
        '`posicion`, `km` y `observacion`, **sin `gasto_id`**, porque el '
        'formulario «Montar una llanta» no tiene ese campo. `grep -n gasto_id '
        'app/static/pwa/flota.js` da cinco resultados y ninguno es el cuerpo '
        'de ese POST. Es «función sin caller», en un parámetro: en producción '
        '`flota_montaje_llanta.gasto_id` va a ser NULL siempre, y la pregunta '
        'que la columna existe para contestar —«¿de qué factura salió esta '
        'llanta?»— no se va a poder contestar nunca. '
        'Y falla PEOR QUE EN SILENCIO: `flota.js:3745` pinta el NULL como '
        '«sin gasto asociado (rotación)», que AFIRMA una causa que nadie '
        'midió. Un montaje que sí vino de una compra y cuyo gasto nadie ató se '
        'lee en pantalla como una rotación.'))
    def test_el_formulario_de_montaje_manda_el_gasto_de_la_compra(self):
        """Detector acotado a UNA función del PWA, no al archivo entero.

        Se recorta el cuerpo de `flotaMontarLlanta` —de su declaración a la
        siguiente declaración de nivel superior— y se exige que `gasto_id`
        aparezca ahí. Acotado a propósito: `gasto_id` sí existe en otras cinco
        líneas del archivo (el taller y el pintado del montaje), así que un
        detector sobre el archivo completo estaría en verde con el hueco
        abierto. Es la forma de «el trinquete que mide una proxy».
        """
        import re
        from pathlib import Path

        js = Path('app/static/pwa/flota.js').read_text(encoding='utf-8')
        i = js.index('async function flotaMontarLlanta(')
        siguiente = re.search(r'\n(?:async function|function|const|/\*\*)',
                              js[i + 10:])
        cuerpo = js[i:i + 10 + siguiente.start()]
        assert 'gasto_id' in cuerpo, (
            'el POST de montaje no manda `gasto_id`: la llanta queda sin la '
            'factura que la compró, y la pantalla lo pinta como «rotación».')

    def test_el_km_de_la_llanta_se_calcula_y_arrastra_su_marca(
            self, app, db, abril):
        """No hay columna `km_acumulado`: se calcula desde las dos lecturas, y
        la marca de confianza viaja con el número."""
        from flota.adaptadores.modelos import MontajeLlanta

        m = MontajeLlanta.query.get(abril['montaje_ids'][0])
        adaptador_llantas.desmontar(
            montaje_id=m.id, km=202000, motivo='desgaste_normal',
            desmontada_por_usuario_id=abril['usuario_id'],
            ts=datetime(2026, 4, 30, 19, 0))
        _paso_por_la_cola(abril['vehiculo_id'], abril['usuario_id'])

        km, marca = adaptador_llantas.km_de_llanta(m.llanta_id)
        assert km == 800                     # 202.000 − 201.200
        assert marca == 'verificada'

    def test_con_las_dos_lecturas_dudosas_el_km_de_la_llanta_es_sin_dato(
            self, app, db, flota):
        """Sin pasar por la cola, el tramo no es un número. Es el mismo
        contrato que el CPK, escrito en otra fase — y es la comprobación de que
        las dos mitades dicen lo mismo."""
        v, u = flota['vehiculo_id'], flota['usuario_id']
        ll = adaptador_llantas.dar_de_alta(
            codigo='E2E-LL-DUDOSA', medida='215/75R17.5',
            registrada_por_usuario_id=u)
        m = adaptador_llantas.montar(
            llanta_id=ll.id, vehiculo_id=v, posicion=1, km=200000,
            montada_por_usuario_id=u, ts=datetime(2026, 4, 1, 13, 0))
        adaptador_llantas.desmontar(
            montaje_id=m.id, km=201000, motivo='rotacion',
            desmontada_por_usuario_id=u, ts=datetime(2026, 4, 20, 13, 0))

        km, marca = adaptador_llantas.km_de_llanta(ll.id)
        assert km == SIN_DATO
        assert marca == SIN_DATO


# ══════════════════════════════════════════════════════════════════════════
# CRUCE fase 3 × odómetro — «unos N días» sobre lecturas dudosas
# ══════════════════════════════════════════════════════════════════════════

class TestElPreventivoNoPublicaUnRitmoQueNadiePuedeRespaldar:
    """*«Un km/día calculado sobre lecturas dudosas no puede publicarse como
    firme»*. El dominio lo respeta; lo que hay que medir es si sobrevive hasta
    quien lo lee."""

    @pytest.fixture
    def con_plan(self, app, db, abril):
        """El plan sembrado desde la ficha, con línea base registrada.

        `distribucion_km_cambio = 60.000` y la última ejecución a 200.000 dejan
        el próximo cambio en 260.000: quedan 58.000 km. Al ritmo medido de
        abril eso es mucho más que `DIAS_AVISO_PREVENTIVO`, así que la tarea
        queda `al_dia` con `dias_estimados` publicado.
        """
        v, u = abril['vehiculo_id'], abril['usuario_id']
        adaptador_prev.sembrar_desde_ficha(v, ahora=datetime(2026, 4, 1, 12, 0))
        from flota.adaptadores.modelos import PlanTarea
        plan = PlanTarea.query.filter_by(vehiculo_id=v,
                                         tipo='distribucion').one()
        adaptador_prev.registrar_ejecucion(
            plan_id=plan.id, km=202000, usuario_id=u,
            taller='Serviteca', ts=datetime(2026, 4, 30, 19, 0))
        _paso_por_la_cola(v, u)
        return dict(abril, plan_id=plan.id)

    def test_el_ritmo_de_abril_es_medible_y_viene_marcado(self, app, db, con_plan):
        ritmo = adaptador_prev.ritmo_de(con_plan['vehiculo_id'])
        assert ritmo.km_dia != SIN_DATO
        assert str(ritmo.marca) in ('Confianza.VERIFICADA', 'verificada')

    def test_el_ritmo_es_SIN_DATO_si_los_dos_extremos_son_dudosos(
            self, app, db, flota):
        """El caso que importa: sin cola de verificación no hay km/día, y por
        lo tanto no hay «unos N días»."""
        v, u = flota['vehiculo_id'], flota['usuario_id']
        for km, ts in ((200000, datetime(2026, 4, 1, 13, 0)),
                       (202000, datetime(2026, 4, 30, 13, 0))):
            adaptador_gastos.registrar_tanqueo(
                vehiculo_id=v, fecha=ts.date(), valor='168000', galones='12',
                tanque='lleno', estacion='Terpel', km=km, proveedor='Terpel',
                origen_costo='tarjeta_convenio',
                registrado_por_usuario_id=u, ts=ts)

        ritmo = adaptador_prev.ritmo_de(v)
        assert ritmo.km_dia == SIN_DATO
        assert ritmo.motivo is not None

        adaptador_prev.sembrar_desde_ficha(v, ahora=datetime(2026, 4, 1, 12, 0))
        from flota.adaptadores.modelos import PlanTarea
        plan = PlanTarea.query.filter_by(vehiculo_id=v,
                                         tipo='distribucion').one()
        adaptador_prev.registrar_ejecucion(
            plan_id=plan.id, km=202000, usuario_id=u,
            ts=datetime(2026, 4, 30, 14, 0))

        d = [t for t in adaptador_prev.diagnostico_de(v)
             if t['tipo'] == 'distribucion'][0]
        # Sin ritmo NO se degrada a `por_vencer` ni a alarma: se queda en lo
        # que el odómetro afirma, con el «cuándo» declarado como sin_dato.
        assert d['dias_estimados'] == SIN_DATO
        assert d['estado'] == 'al_dia'

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO — campo calculado sin un solo consumidor. '
        '`Diagnostico.marca_ritmo` existe en el dominio, se llena en cada '
        'diagnóstico… y `preventivo.diagnostico_de` arma el dict de la '
        'pantalla SIN esa clave. `grep -rn marca_ritmo` da dos apariciones, '
        'las dos dentro de `flota/dominio/preventivo.py`: la declaración y la '
        'asignación. Es la forma de «función sin caller», con un dato. '
        'El docstring de `dias_hasta` dice literalmente «esa viaja aparte, en '
        '`ritmo.marca`, y quien lo pinte tiene que pintarla». '
        'CONTRASTE que lo vuelve claro: el CPK devuelve `{cpk, marca, ...}` '
        'en el MISMO dict; acá el número y su marca se separan. La marca '
        'sobrevive en `/flota/preventivo/<placa>.ritmo` —un campo hermano, por '
        'vehículo— y NO sobrevive en los otros tres endpoints que devuelven '
        '`tareas` (`sembrar`, `ejecucion`, `fijar`), que no traen ritmo '
        'ninguno. Y el estado `por_vencer` —que alimenta `tareas_por_vencer` '
        'en el health— se decide con ese ritmo sin que el conteo diga si era '
        'dudoso. Falla EN SILENCIO.'))
    def test_los_dias_estimados_salen_con_la_marca_del_ritmo(
            self, app, db, con_plan):
        d = [t for t in adaptador_prev.diagnostico_de(con_plan['vehiculo_id'])
             if t['tipo'] == 'distribucion'][0]
        assert d['dias_estimados'] != SIN_DATO
        assert 'marca_ritmo' in d

    def test_un_dia_estimado_sobre_un_ritmo_DUDOSO_igual_se_publica_firme(
            self, app, db, flota):
        """La consecuencia medida del defecto de arriba, escrita como hecho.

        Un extremo verificado y el otro dudoso dan `marca = dudosa` y un km/día
        REAL — así que `dias_estimados` sale con un número. Este test **pasa
        hoy** y no es una celebración: es la evidencia de que el número flojo se
        publica exactamente igual que el firme.
        """
        from flota.adaptadores import verificacion
        from flota.adaptadores.modelos import PlanTarea

        v, u = flota['vehiculo_id'], flota['usuario_id']
        for km, ts in ((200000, datetime(2026, 4, 1, 13, 0)),
                       (202000, datetime(2026, 4, 30, 13, 0))):
            adaptador_gastos.registrar_tanqueo(
                vehiculo_id=v, fecha=ts.date(), valor='168000', galones='12',
                tanque='lleno', estacion='Terpel', km=km, proveedor='Terpel',
                origen_costo='tarjeta_convenio',
                registrado_por_usuario_id=u, ts=ts)
        # Solo la PRIMERA pasa por la cola: el tramo queda con un extremo
        # sólido y uno dudoso, que es el caso que sí produce número.
        primera = min((l for l, _p in verificacion.pendientes()
                       if l.vehiculo_id == v), key=lambda l: l.valor_km)
        verificacion.verificar(lectura_id=primera.id, usuario_id=u)

        ritmo = adaptador_prev.ritmo_de(v)
        assert str(ritmo.marca) in ('Confianza.DUDOSA', 'dudosa')
        assert ritmo.km_dia != SIN_DATO

        adaptador_prev.sembrar_desde_ficha(v, ahora=datetime(2026, 4, 1, 12, 0))
        plan = PlanTarea.query.filter_by(vehiculo_id=v,
                                         tipo='distribucion').one()
        adaptador_prev.registrar_ejecucion(
            plan_id=plan.id, km=202000, usuario_id=u,
            ts=datetime(2026, 4, 30, 14, 0))

        d = [t for t in adaptador_prev.diagnostico_de(v)
             if t['tipo'] == 'distribucion'][0]
        assert isinstance(d['dias_estimados'], int)
        # Y no hay una sola clave en el dict que diga que ese número salió de
        # un ritmo dudoso.
        assert not any('marca' in k or 'confianza' in k for k in d)


# ══════════════════════════════════════════════════════════════════════════
# CRUCE hallazgo → OT → cierre
# ══════════════════════════════════════════════════════════════════════════

class TestElDanoSeCierraPorLaOrdenDeTrabajo:

    @pytest.fixture
    def con_dano(self, app, db, flota):
        from flota.adaptadores import hallazgos as adaptador_hallazgos

        v, u = flota['vehiculo_id'], flota['usuario_id']
        h = adaptador_hallazgos.reportar(
            vehiculo_id=v, descripcion='Fuga de aceite en el motor',
            criticidad='mayor', reportado_por_usuario_id=u, km=200000,
            ts=datetime(2026, 4, 1, 13, 0))
        return dict(flota, hallazgo_id=h.id)

    def test_cerrar_la_OT_cierra_el_hallazgo_por_su_propia_puerta(
            self, app, db, con_dano):
        from flota.adaptadores.modelos import Hallazgo
        from flota.dominio.hallazgo import EstadoHallazgo

        v, u = con_dano['vehiculo_id'], con_dano['usuario_id']
        orden = adaptador_taller.abrir(
            vehiculo_id=v, tipo='correctiva', taller='Serviteca',
            descripcion='Sellar la fuga', km=200100,
            abierta_por_usuario_id=u, hallazgo_id=con_dano['hallazgo_id'],
            ts=datetime(2026, 4, 2, 13, 0))
        adaptador_taller.registrar_intervencion(
            orden_trabajo_id=orden.id, sistema='motor',
            garantia_declarada='sin_dato', registrada_por_usuario_id=u,
            ts=datetime(2026, 4, 2, 14, 0))
        adaptador_taller.cerrar(orden_trabajo_id=orden.id, usuario_id=u,
                                cerrar_hallazgo=True,
                                ts=datetime(2026, 4, 3, 13, 0))

        h = Hallazgo.query.get(con_dano['hallazgo_id'])
        assert h.estado == EstadoHallazgo.CERRADO
        # `cerrado_ts` es cuando el vehículo vuelve reparado, y de ahí sale
        # `dias_hallazgo_abierto`. Una segunda vía de cierre es siempre la que
        # se olvida de moverlo.
        assert h.cerrado_ts == datetime(2026, 4, 3, 13, 0)
        assert h.cerrado_por_usuario_id == u

    def test_cerrar_la_OT_sin_pedirlo_deja_el_dano_abierto(
            self, app, db, con_dano):
        """`cerrar_hallazgo` nace en `False`: un camión puede volver del taller
        con el daño todavía abierto —faltó un repuesto—, y cerrarlo por defecto
        inventaría una reparación."""
        from flota.adaptadores.modelos import Hallazgo
        from flota.dominio.hallazgo import EstadoHallazgo

        v, u = con_dano['vehiculo_id'], con_dano['usuario_id']
        orden = adaptador_taller.abrir(
            vehiculo_id=v, tipo='correctiva', taller='Serviteca',
            descripcion='Sellar la fuga', km=200100,
            abierta_por_usuario_id=u, hallazgo_id=con_dano['hallazgo_id'],
            ts=datetime(2026, 4, 2, 13, 0))
        adaptador_taller.registrar_intervencion(
            orden_trabajo_id=orden.id, sistema='motor',
            garantia_declarada='sin_dato', registrada_por_usuario_id=u,
            ts=datetime(2026, 4, 2, 14, 0))
        adaptador_taller.cerrar(orden_trabajo_id=orden.id, usuario_id=u,
                                ts=datetime(2026, 4, 3, 13, 0))

        assert (Hallazgo.query.get(con_dano['hallazgo_id']).estado
                == EstadoHallazgo.ABIERTO)

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO, ya señalado por la auditoría y todavía cierto. `flota/'
        'CLAUDE.md` regla 6 dice: «No existe transición `abierto → cerrado` '
        'directa: se cierra por OT, o se `descarta` con motivo escrito». '
        '`hallazgos.cerrar()` NO exige orden de trabajo, NO exige evidencia y '
        'NI SIQUIERA exige nota — y está expuesta como `POST /flota/hallazgos/'
        '<id>/cerrar`. Es la segunda vía que la regla 6 prohíbe y es la forma '
        'más barata de poner `hallazgos_vencidos` en cero (regla 11): cerrar '
        'los daños. Falla EN SILENCIO: devuelve 200 y el indicador queda '
        'limpio. Contrasta con `descartar`, que sí exige motivo escrito con '
        'CHECK detrás — la salida cara está protegida y la barata no.'))
    def test_cerrar_un_dano_sin_orden_de_trabajo_no_deberia_poder(
            self, app, db, con_dano):
        from flota.adaptadores import hallazgos as adaptador_hallazgos
        from flota.dominio.errores import ErrorFlota

        with pytest.raises(ErrorFlota):
            adaptador_hallazgos.cerrar(hallazgo_id=con_dano['hallazgo_id'],
                                       usuario_id=con_dano['usuario_id'])

    def test_si_alguien_ya_cerro_el_dano_la_OT_NO_se_cierra(
            self, app, db, con_dano):
        """Las dos cosas van en la misma transacción, o ninguna.

        Es la comprobación de que `taller.cerrar` pasa de verdad por
        `hallazgos.cerrar` y no por un `UPDATE` propio: la segunda vía no
        levantaría acá, y quien pidió las dos cosas se iría creyendo que
        ocurrieron.
        """
        from flota.adaptadores import hallazgos as adaptador_hallazgos
        from flota.adaptadores.modelos import OrdenTrabajo
        from flota.dominio.errores import ErrorFlota

        v, u = con_dano['vehiculo_id'], con_dano['usuario_id']
        orden = adaptador_taller.abrir(
            vehiculo_id=v, tipo='correctiva', taller='Serviteca',
            descripcion='Sellar la fuga', km=200100,
            abierta_por_usuario_id=u, hallazgo_id=con_dano['hallazgo_id'],
            ts=datetime(2026, 4, 2, 13, 0))
        adaptador_taller.registrar_intervencion(
            orden_trabajo_id=orden.id, sistema='motor',
            garantia_declarada='sin_dato', registrada_por_usuario_id=u,
            ts=datetime(2026, 4, 2, 14, 0))
        # Alguien lo cerró desde su pantalla mientras el camión estaba adentro.
        adaptador_hallazgos.cerrar(hallazgo_id=con_dano['hallazgo_id'],
                                   usuario_id=u, ts=datetime(2026, 4, 2, 15, 0))

        with pytest.raises(ErrorFlota):
            adaptador_taller.cerrar(orden_trabajo_id=orden.id, usuario_id=u,
                                    cerrar_hallazgo=True,
                                    ts=datetime(2026, 4, 3, 13, 0))
        db.session.expire_all()
        assert OrdenTrabajo.query.get(orden.id).estado == 'abierta'

    def test_la_via_barata_existe_y_deja_el_indicador_limpio(
            self, app, db, con_dano):
        """La medición del defecto de arriba, escrita como hecho.

        **Pasa hoy y no es una celebración**: es la evidencia de que un daño se
        cierra sin OT, sin evidencia y sin una palabra escrita.
        """
        from flota.adaptadores import hallazgos as adaptador_hallazgos
        from flota.dominio.hallazgo import EstadoHallazgo

        h = adaptador_hallazgos.cerrar(hallazgo_id=con_dano['hallazgo_id'],
                                       usuario_id=con_dano['usuario_id'])
        assert h.estado == EstadoHallazgo.CERRADO
        assert h.motivo_cierre is None
        from flota.adaptadores.modelos import OrdenTrabajo
        assert OrdenTrabajo.query.filter_by(
            hallazgo_id=con_dano['hallazgo_id']).count() == 0


class TestLaFacturaDelTallerCaeEnElMesDeSuFECHA:
    """**Mide una consecuencia del diseño, no un bug.** Va escrita porque nadie
    la había ejercido y porque decide plata.

    El propio módulo lo dice: *«la factura del taller se digita treinta días
    después, en una oficina»*, y el formulario pide literalmente «Fecha de la
    factura». El kilometraje se ancla al día del TRABAJO (correcto: contra ése
    se calcula la garantía) y los pesos entran el día de la FACTURA.

    Consecuencia: el trabajo de abril con factura de mayo **no está en el CPK
    de abril** y sí en el de mayo, contra los kilómetros de mayo. No es un
    defecto del código —el canon §4 dice que lo que no cubre un período entra
    el día en que ocurre— pero sí es una propiedad que hay que conocer antes de
    leer un CPK mensual: en un mes con dos facturas atrasadas, el número dice
    más de cuándo contabilidad digitó que de cómo rodó el camión.
    """

    def test_el_trabajo_de_abril_con_factura_de_mayo_no_entra_en_abril(
            self, app, db, flota):
        from flota.adaptadores.modelos import LecturaOdometro

        v, u = flota['vehiculo_id'], flota['usuario_id']
        db.session.add(LecturaOdometro(
            vehiculo_id=v, valor_km=200000, ts=datetime(2026, 4, 1, 12, 0),
            origen='cierre_dia', autor_usuario_id=u))
        db.session.commit()

        orden = adaptador_taller.abrir(
            vehiculo_id=v, tipo='correctiva', taller='Serviteca',
            descripcion='Cambio de pastillas', km=200500,
            abierta_por_usuario_id=u, ts=datetime(2026, 4, 10, 13, 0))
        interv = adaptador_taller.registrar_intervencion(
            orden_trabajo_id=orden.id, sistema='frenos',
            garantia_declarada='sin_dato', registrada_por_usuario_id=u,
            ts=datetime(2026, 4, 10, 14, 0))
        db.session.add(LecturaOdometro(
            vehiculo_id=v, valor_km=201000, ts=datetime(2026, 4, 30, 18, 0),
            origen='cierre_dia', autor_usuario_id=u))
        db.session.commit()
        # La factura llega el 20 de mayo. El gasto se ancla al odómetro de
        # ABRIL (200.500, la lectura de la OT) y su `fecha` es de MAYO.
        gasto = adaptador_taller.registrar_factura(
            orden_trabajo_id=orden.id, intervencion_ids=[interv.id],
            categoria='mantenimiento', fecha=date(2026, 5, 20),
            valor='350000', proveedor='Serviteca',
            origen_costo='credito_proveedor', registrado_por_usuario_id=u,
            ts=datetime(2026, 5, 20, 13, 0))
        _paso_por_la_cola(v, u)

        assert gasto.lectura.valor_km == 200500          # km de abril
        abril_r = adaptador_gastos.cpk_de(v, ABRIL_DESDE, ABRIL_HASTA)
        assert abril_r['km'] == 1000
        # Los $350.000 del trabajo de abril NO están en abril.
        assert abril_r['pesos'] == Decimal('0')
        assert abril_r['cpk'] == Decimal('0')            # cero MEDIDO, no sin_dato

        mayo_r = adaptador_gastos.cpk_de(v, date(2026, 5, 1), date(2026, 5, 31))
        assert mayo_r['pesos'] == Decimal('350000')
        # …y mayo no tiene ni un kilómetro que los sostenga.
        assert mayo_r['km'] == 0
        assert mayo_r['cpk'] == SIN_DATO


# ══════════════════════════════════════════════════════════════════════════
# EL CRUCE QUE NO EXISTE — fase 2 (taller) × fase 3 (preventivo)
# ══════════════════════════════════════════════════════════════════════════

class TestElTallerYElPreventivoNoSeHablan:
    """**Este bloque mide un hueco, no lo prueba resuelto.**

    Hay dos formas de registrar «se hizo el mantenimiento preventivo» y
    ninguna sabe de la otra:

    | Vía | Qué escribe | Efecto en el plan |
    |---|---|---|
    | `preventivo.registrar_ejecucion` | `flota_ejecucion_tarea` | mueve la línea base |
    | `taller.abrir(tipo='preventiva') + registrar_intervencion` | `flota_orden_trabajo` + `flota_intervencion` | **ninguno** |

    `flota_ejecucion_tarea` no tiene `orden_trabajo_id` y `registrar_ejecucion`
    no lo recibe. No es un `if` olvidado: **no hay dato que los una.** La
    intervención declara un `sistema` (`motor`) y la tarea declara un `tipo`
    (`distribucion`), que no son el mismo vocabulario.

    Queda como test que MIDE y no como `xfail`: exigir que cerrar la OT mueva
    la línea base sería inventar acá la correspondencia sistema→tarea, que es
    una decisión de diseño y no un arreglo. Va al informe.
    """

    def test_una_OT_preventiva_cerrada_deja_el_plan_diciendo_que_nunca_se_hizo(
            self, app, db, abril):
        from flota.adaptadores.modelos import PlanTarea

        v, u = abril['vehiculo_id'], abril['usuario_id']
        adaptador_prev.sembrar_desde_ficha(v, ahora=datetime(2026, 4, 1, 12, 0))
        assert PlanTarea.query.filter_by(vehiculo_id=v,
                                         tipo='distribucion').one() is not None

        orden = adaptador_taller.abrir(
            vehiculo_id=v, tipo='preventiva', taller='Serviteca del Sur',
            descripcion='Cambio de correa de distribución y tensor',
            km=202000, abierta_por_usuario_id=u,
            ts=datetime(2026, 5, 2, 13, 0))
        adaptador_taller.registrar_intervencion(
            orden_trabajo_id=orden.id, sistema='motor',
            garantia_declarada='si', garantia_meses=6,
            registrada_por_usuario_id=u, ts=datetime(2026, 5, 2, 14, 0))
        adaptador_taller.cerrar(orden_trabajo_id=orden.id, usuario_id=u,
                                ts=datetime(2026, 5, 2, 15, 0))

        d = [t for t in adaptador_prev.diagnostico_de(v)
             if t['tipo'] == 'distribucion'][0]
        # El taller tiene el trabajo escrito y el plan sigue diciendo que la
        # correa nunca se cambió. El costo del hueco es el mismo que la
        # garantía existe para evitar en la fase 2: mandar el camión otra vez.
        assert d['estado'] == 'sin_linea_base'
        assert d['ultima_ejecucion_km'] == SIN_DATO

    def test_no_existe_ninguna_columna_que_una_una_ejecucion_con_una_OT(
            self, app):
        """El hueco es de esquema, no de código: no hay dónde escribirlo.

        Si alguien construye el puente, este test se cae y hay que actualizar
        el bloque — que es exactamente lo que se quiere.
        """
        from flota.adaptadores.modelos import EjecucionTarea

        columnas = {c.name for c in EjecucionTarea.__table__.columns}
        assert 'orden_trabajo_id' not in columnas
        assert 'intervencion_id' not in columnas


# ══════════════════════════════════════════════════════════════════════════
# La ventana se mide en DÍA OPERATIVO (Bogotá), no en UTC
# ══════════════════════════════════════════════════════════════════════════

class TestLaVentanaEsDiaOperativoConTodasLasFasesCargadas:
    """Ya hay tests de la frontera de mes sobre tanqueos solos. Acá se mide con
    las tres extremidades adentro, que es donde el corrimiento de cinco horas
    mueve más plata."""

    def test_la_lectura_de_las_8pm_del_30_cuenta_en_ABRIL(self, app, db, abril):
        """`2026-05-01 01:00 UTC` es el 30 de abril a las 8 p.m. en Bogotá.

        Si la ventana se midiera en UTC, esa lectura caería en mayo y el tramo
        de abril se quedaría corto: menos kilómetros con los mismos pesos, o
        sea un CPK inflado, y con la marca `verificada` al lado.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.add(LecturaOdometro(
            vehiculo_id=abril['vehiculo_id'], valor_km=202500,
            ts=datetime(2026, 5, 1, 1, 0), origen='cierre_dia',
            autor_usuario_id=abril['usuario_id']))
        db.session.commit()
        _paso_por_la_cola(abril['vehiculo_id'], abril['usuario_id'])

        r = adaptador_gastos.cpk_de(abril['vehiculo_id'],
                                    ABRIL_DESDE, ABRIL_HASTA)
        assert r['km'] == 2500
        assert r['pesos'] == PESOS_ESPERADOS

    def test_la_misma_lectura_NO_cuenta_en_mayo(self, app, db, abril):
        """El otro lado: el detector también tiene que no disparar. En UTC esa
        lectura es del 1 de mayo, y si mayo la contara el mismo kilómetro
        estaría en los dos meses."""
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.add(LecturaOdometro(
            vehiculo_id=abril['vehiculo_id'], valor_km=202500,
            ts=datetime(2026, 5, 1, 1, 0), origen='cierre_dia',
            autor_usuario_id=abril['usuario_id']))
        db.session.commit()

        r = adaptador_gastos.cpk_de(abril['vehiculo_id'],
                                    date(2026, 5, 1), date(2026, 5, 31))
        # Una sola lectura de mayo (ninguna, en realidad) no delimita un tramo.
        assert r['km'] == 0
        assert r['cpk'] == SIN_DATO
