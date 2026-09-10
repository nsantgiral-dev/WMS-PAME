"""Lo que el servidor dice y la pantalla escuchaba: los tres casos.

## El patrón, medido

Un barrido por AST el 2026-09-03 contó **11 de 95 claves** que la API de flota
devuelve y ningún JS lee. Tres eran de la misma familia y las tres le costaban
algo a una persona concreta:

| clave | quién la necesitaba | qué veía en cambio |
|---|---|---|
| `estado_vehiculo` *(no existía)* | el conductor a las 5 a.m. | su placa y su kilometraje |
| `confianza` / `motivo_dudosa` | quien registra un odómetro | «Lectura registrada ✓» en verde |
| `tu_rol` / `roles_permitidos` | quien recibe un 403 | «Sin permiso» a secas |

Es la firma del módulo entero: **se mide, se publica, y no hay lector.**

## Qué NO hace este cambio

**No bloquea nada.** El estado informa; el camión sale igual. Es la secuencia
obligatoria —medir, corregir, imponer— y la regla 1: dejar un camión en el patio
por una pantalla es cómo la operación desmonta el sistema en 48 horas. Lo que
cierra es la frase «no sabía».
"""
from datetime import date, datetime, timedelta

import pytest

_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


@pytest.fixture
def mundo(db, almacen):
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.traspaso import traspasar
    from flota.dominio.valores import CustodioTipo

    v = Vehiculo(placa='CVE100', tipo='NHR', activo=True)
    u = Usuario(nombre='Turno A', email='cve@t.co', rol='conductor',
                almacen_id=almacen.id, activo=True,
                password_hash=generate_password_hash('x'))
    db.session.add_all([v, u])
    db.session.flush()
    c = Conductor(nombre='Turno A', cedula='CVE-1', activo=True, usuario_id=u.id)
    db.session.add(c)
    db.session.commit()

    traspasar(vehiculo_id=v.id, km=1000, registrado_por_usuario_id=u.id,
              custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=c.id)
    return {'veh': v.id, 'placa': v.placa, 'usr': u.id, 'cond': c.id, 'db': db,
            't': create_access_token(identity=str(u.id))}


def _turno(client, mundo):
    r = client.get('/flota/conductor/mi-turno', headers=_auth(mundo['t']))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


class TestElConductorVeElEstadoDeSuCamion:
    def test_un_camion_sano_no_le_grita(self, client, mundo):
        """La otra dirección primero: si el bloque apareciera siempre, se dejaría
        de leer — la lección de los 639 avisos conocidos."""
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgos_abiertos'] == 0
        assert e['hallazgos_vencidos'] == 0
        assert e['hallazgo_peor'] is None
        assert e['documentos_vencidos'] == []

    def test_ve_el_dano_que_el_mismo_reporto(self, client, mundo):
        """**El caso.** Hasta hoy reportaba un daño y al día siguiente su
        pantalla no lo mencionaba."""
        from flota.adaptadores import hallazgos

        hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='bloqueante',
                           descripcion='Freno de servicio sin presión', km=1200,
                           reportado_por_usuario_id=mundo['usr'])
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgos_abiertos'] == 1
        assert e['hallazgo_peor']['criticidad'] == 'bloqueante'
        assert 'Freno' in e['hallazgo_peor']['descripcion']

    def test_el_peor_es_el_bloqueante_y_no_el_primero_que_salga(
            self, client, mundo):
        """Un contador dice cuántos; el conductor necesita saber CUÁL mirar."""
        from flota.adaptadores import hallazgos

        hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='menor',
                           descripcion='rayón', km=1100,
                           reportado_por_usuario_id=mundo['usr'])
        hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='bloqueante',
                           descripcion='dirección con juego', km=1200,
                           reportado_por_usuario_id=mundo['usr'])
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgo_peor']['criticidad'] == 'bloqueante'

    def test_un_dano_vencido_se_cuenta_aparte(self, client, mundo):
        from flota.adaptadores import hallazgos
        from flota.adaptadores.modelos import Hallazgo

        h = hallazgos.reportar(vehiculo_id=mundo['veh'], criticidad='mayor',
                               descripcion='fuga', km=1100,
                               reportado_por_usuario_id=mundo['usr'])
        Hallazgo.query.get(h.id).fecha_limite = datetime.utcnow() - timedelta(days=2)
        mundo['db'].session.commit()
        e = _turno(client, mundo)['estado_vehiculo']
        assert e['hallazgos_vencidos'] == 1
        assert e['hallazgo_peor']['vencido'] is True

    def test_ve_el_preventivo_vencido(self, client, mundo):
        """**«Un daño en la correa lo paga el que va manejando.»**

        Era el argumento con el que el conductor tenía acceso al plan completo
        —nueve tareas con intervalos y procedencias—. El argumento es correcto y
        el canal no: su panel dura dos minutos a las 5 a.m. Lo vencido le llega
        acá; el navegador de expedientes es de escritorio.
        """
        e = _turno(client, mundo)['estado_vehiculo']
        assert 'preventivo_vencido' in e
        assert isinstance(e['preventivo_vencido'], list)

    def test_una_tarea_nunca_ejecutada_NO_le_aparece_como_vencida(
            self, client, mundo):
        """Regla 4. Sin línea base la tarea no está al día ni vencida — decirle
        que «venció» algo que nadie hizo nunca es inventarle una deuda. El
        health la cuenta aparte como `tareas_sin_linea_base`."""
        from flota.adaptadores import preventivo
        from flota.adaptadores.modelos import FichaTecnica, PlanTarea

        # El plan se siembra DESDE la ficha, así que sin ficha no hay tareas y
        # el test no probaría nada. Con `distribucion_km_cambio` cargado nace la
        # tarea de la correa — la pieza que llevaba meses en la base sin lector.
        mundo['db'].session.add(FichaTecnica(
            vehiculo_id=mundo['veh'], posiciones_llanta=6, km_inicial=1000,
            km_inicial_ts=datetime.utcnow(), distribucion='correa',
            distribucion_fuente='manual_fabricante',
            distribucion_km_cambio=60000))
        mundo['db'].session.commit()
        preventivo.sembrar_desde_ficha(mundo['veh'])
        assert PlanTarea.query.filter_by(vehiculo_id=mundo['veh']).count() > 0, (
            'el escenario no sembró ninguna tarea: el test no probaría nada')

        e = _turno(client, mundo)['estado_vehiculo']
        assert e['preventivo_vencido'] == [], (
            'una tarea sin línea base salió como vencida: se le está inventando '
            'una deuda al conductor por algo que nadie ejecutó nunca')

    def test_ve_un_documento_vencido(self, client, mundo):
        """Una tecnomecánica vencida es un camión que no debería circular, y el
        que lo maneja no se enteraba."""
        from flota.adaptadores.modelos import DocumentoVehiculo

        mundo['db'].session.add(DocumentoVehiculo(
            vehiculo_id=mundo['veh'], tipo='rtm', numero='X-1',
            entidad='CDA Neiva', fecha_expedicion=date(2024, 11, 11),
            fecha_vencimiento=date(2025, 11, 11), estado='vigente'))
        mundo['db'].session.commit()
        e = _turno(client, mundo)['estado_vehiculo']
        assert [d['tipo'] for d in e['documentos_vencidos']] == ['rtm']

    def test_sin_vehiculo_el_estado_es_None_y_no_un_cero(self, client, db, almacen):
        """Regla 4: «no hay a qué mirarle el estado» no es «está todo bien»."""
        from flask_jwt_extended import create_access_token
        from werkzeug.security import generate_password_hash

        from app.models.conductor import Conductor
        from app.models.usuario import Usuario

        u = Usuario(nombre='Sin camión', email='sinv@t.co', rol='conductor',
                    almacen_id=almacen.id, activo=True,
                    password_hash=generate_password_hash('x'))
        db.session.add(u)
        db.session.flush()
        db.session.add(Conductor(nombre='Sin camión', cedula='SV-1',
                                 activo=True, usuario_id=u.id))
        db.session.commit()
        r = client.get('/flota/conductor/mi-turno',
                       headers=_auth(create_access_token(identity=str(u.id))))
        assert r.get_json()['estado_vehiculo'] is None


class TestHabilitaDespachoLlegaAQuienDecide:
    """`habilita_despacho` se calculaba, se publicaba, y su único lector era un
    mensaje que desaparecía. Ahora le llega al conductor."""

    def test_sin_inspeccion_lo_dice_sin_afirmar_que_esta_bien(self, client, mundo):
        i = _turno(client, mundo)['estado_vehiculo']['inspeccion_de_hoy']
        assert i['hecha'] is False
        assert i['habilita_despacho'] is None, (
            'un `False` diría que la inspección salió mal; lo que pasa es que '
            'no se hizo, y son cosas distintas (regla 4)')

    def test_una_inspeccion_no_apta_viaja_como_tal(self, client, mundo):
        """Con el adaptador REAL y no con una fila a mano.

        `flota_inspeccion` tiene seis CHECK que ligan veredicto, ítems sin dato
        y bloqueantes; una fila construida a mano o los adivina o los viola —y
        si los adivina bien, prueba que la base acepta esa fila, no que el
        sistema la produzca. Es la regla del arnés de `tests/flujo/`: cada etapa
        se avanza llamando al servicio que la operación llama.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores import catalogo, inspecciones

        catalogo.sembrar(mundo['db'])
        vehiculo = Vehiculo.query.get(mundo['veh'])
        items = inspecciones.items_del_dia_de(vehiculo, __import__(
            'app.utils.fecha', fromlist=['dia_operativo']).dia_operativo())
        bloqueante = next(i for i in items if i.criticidad == 'bloqueante')

        inspecciones.registrar(
            vehiculo_id=mundo['veh'], tipo_vehiculo_obj=vehiculo, km=1500,
            inspeccionada_por_usuario_id=mundo['usr'],
            respuestas=[{'item_id': i.id,
                         'respuesta': 'no_apto' if i.id == bloqueante.id else 'optimo'}
                        for i in items],
            segundos_llenado=180)

        i = _turno(client, mundo)['estado_vehiculo']['inspeccion_de_hoy']
        assert i['hecha'] is True
        assert i['veredicto'] == 'no_apto'
        assert i['habilita_despacho'] is False, (
            'un bloqueante en no_apto tiene que llegarle al conductor: el '
            'veredicto se calculaba y su único lector era un toast')

    def test_una_inspeccion_apta_SI_habilita(self, client, mundo):
        """La otra dirección: si `habilita_despacho` fuera siempre False, el
        campo no distinguiría nada y el conductor aprendería a ignorarlo."""
        from app.models.vehiculo import Vehiculo
        from app.utils.fecha import dia_operativo
        from flota.adaptadores import catalogo, inspecciones

        catalogo.sembrar(mundo['db'])
        vehiculo = Vehiculo.query.get(mundo['veh'])
        items = inspecciones.items_del_dia_de(vehiculo, dia_operativo())
        inspecciones.registrar(
            vehiculo_id=mundo['veh'], tipo_vehiculo_obj=vehiculo, km=1500,
            inspeccionada_por_usuario_id=mundo['usr'],
            respuestas=[{'item_id': i.id, 'respuesta': 'optimo'} for i in items],
            segundos_llenado=180)

        i = _turno(client, mundo)['estado_vehiculo']['inspeccion_de_hoy']
        assert i['veredicto'] == 'apto'
        assert i['habilita_despacho'] is True


class TestElErrorDicePorQue:
    def test_un_403_de_flota_trae_el_rol_que_haria_falta(self, client, mundo):
        """El docstring de `exige` promete que el 403 diga qué hace falta.
        Lo dice — y hasta hoy la pantalla lo tiraba."""
        r = client.post('/flota/gastos', json={}, headers=_auth(mundo['t']))
        assert r.status_code == 403
        d = r.get_json()
        assert d['tu_rol'] == 'conductor'
        assert d['roles_permitidos']


class TestElConductorVeElRendimientoDeSuCamion:
    """El cuarto de la familia: `piso-conductor.md:149` promete «Rendimiento
    km/galón del vehículo» como señal de que está haciendo bien el trabajo desde
    el 2026-08-04, y el sistema se lo negaba.

    **km/galón y no CPK.** El CPK divide por pesos que él no controla —pólizas,
    impuestos, multas, una entrada a taller— y un número que alguien no puede
    mover es un número que aprende a ignorar.
    """

    def test_sin_tanqueos_dice_QUE_FALTA_y_no_muestra_un_numero(
            self, client, mundo):
        """Tres estados, no dos. «Midiendo» no es `sin_dato`: uno solo necesita
        que pase el tiempo, el otro necesita que alguien marque el tanque."""
        r = _turno(client, mundo)['rendimiento']
        assert r['publicable'] is False
        assert r['km_galon'] == 'sin_dato'
        assert 'tanque lleno' in r['motivo']
        assert r['ventanas'] == 0

    def test_las_ventanas_y_los_tanqueos_perdidos_viajan_SIEMPRE(
            self, client, mundo):
        """La condición 2 del dueño. Un rendimiento sobre 3 de 20 tanqueos y uno
        sobre 19 de 20 se ven idénticos, y el primero no significa nada."""
        r = _turno(client, mundo)['rendimiento']
        assert 'ventanas' in r and 'tanqueos_fuera_por_parcial' in r
        assert 'dias_historia' in r

    def test_la_pantalla_recibe_lo_que_el_numero_NO_afirma(self, client, mundo):
        """Regla 2: el sistema dice cuánto rindió el CAMIÓN. Una ruta con más
        montaña, un filtro tapado y un sifón dan el mismo número."""
        r = _turno(client, mundo)['rendimiento']
        assert 'no a quien maneja' in r['no_afirma']

    def test_NO_viaja_ningun_CPK_ni_ningun_peso_al_conductor(
            self, client, mundo):
        """La decisión del dueño, comprobada sobre la respuesta entera y no solo
        sobre el bloque: un CPK en su pantalla está a un paso de leerse como una
        medida suya, que es lo que la regla 2 prohíbe."""
        import json

        crudo = json.dumps(_turno(client, mundo))
        for prohibido in ('cpk', 'pesos', 'costo_por_kilometro'):
            assert prohibido not in crudo, (
                f'la respuesta del conductor trae {prohibido!r}')

    def test_NO_hay_lista_de_vehiculos_en_el_rendimiento(self, client, mundo):
        """Sin ranking: el endpoint devuelve UN vehículo, el del turno. No hay
        forma de pedir una comparación."""
        r = _turno(client, mundo)['rendimiento']
        assert not any(isinstance(v, list) for v in r.values())

    def test_la_firma_del_dominio_NO_recibe_conductor(self):
        """La regla 2 escrita en el tipo y no en un comentario. Mismo criterio
        que ya protege a `rendimiento_km_galon`."""
        import inspect

        from flota.adaptadores.gastos import rendimiento_publicable_de
        from flota.dominio.costos import rendimiento_publicable

        for fn in (rendimiento_publicable, rendimiento_publicable_de):
            params = ' '.join(inspect.signature(fn).parameters)
            assert 'conductor' not in params and 'usuario' not in params, (
                f'{fn.__name__} recibe a una persona: {params}')

    def test_sin_camion_asignado_devuelve_None_y_no_un_cero(
            self, client, db, almacen):
        """«No tenés camión hoy» no es «tu camión rinde 0»."""
        from flask_jwt_extended import create_access_token
        from werkzeug.security import generate_password_hash

        from app.models.conductor import Conductor
        from app.models.usuario import Usuario

        u = Usuario(nombre='Sin camión', email='sincam@t.co', rol='conductor',
                    almacen_id=almacen.id, activo=True,
                    password_hash=generate_password_hash('x'))
        db.session.add(u)
        db.session.flush()
        db.session.add(Conductor(nombre='Sin camión', cedula='SC-1',
                                 activo=True, usuario_id=u.id))
        db.session.commit()

        d = client.get('/flota/conductor/mi-turno',
                       headers=_auth(create_access_token(identity=str(u.id)))
                       ).get_json()
        assert d['vehiculo_id'] is None
        assert d['rendimiento'] is None


def _sembrar_tanqueos(mundo, *, llenos, dias_entre=15, km_paso=800):
    """Deja el vehículo con `llenos` tanqueos de tanque lleno, espaciados.

    Existe porque **sin esto los tests del rendimiento no probaban nada**: el
    arnés de mutación del 2026-09-04 bajó el umbral de seis ventanas a una,
    borró el umbral de días y le mandó al conductor el número no publicable, y
    las tres mutaciones **sobrevivieron**. Con cero tanqueos el número nunca es
    publicable, así que ningún umbral cambia nada observable.

    Es el detector en una sola dirección: probar que el hueco se ve, sin probar
    nunca que la cifra aparece cuando debe.
    """
    from datetime import date as _date

    from flota.adaptadores import gastos as adaptador

    base = _date(2026, 1, 5)
    km = 10000
    for i in range(llenos):
        adaptador.registrar_tanqueo(
            vehiculo_id=mundo['veh'],
            fecha=base + timedelta(days=dias_entre * i),
            valor='168000', galones='12', tanque='lleno',
            estacion='Terpel Neiva', km=km, proveedor='Terpel',
            origen_costo='tarjeta_convenio',
            registrado_por_usuario_id=mundo['usr'],
            ts=datetime(2026, 1, 5, 10, 0) + timedelta(days=dias_entre * i))
        km += km_paso


class TestLasCuatroCondicionesMUERDEN:
    """Los tests de arriba prueban el hueco; éstos prueban **el umbral**.

    Cuatro mutaciones sobrevivieron el 2026-09-04 —bajar el umbral a una
    ventana, borrar el de días, dejar de contar los tanqueos perdidos, y mandarle
    al conductor el número no publicable— porque el mundo de prueba tenía cero
    tanqueos y el número nunca podía publicarse. Un umbral que nunca se alcanza
    no se puede violar.
    """

    def test_con_SIETE_llenos_y_90_dias_el_numero_SI_se_publica(
            self, client, db, mundo):
        """La dirección que faltaba. Sin esto, «no publicable» podría ser la
        respuesta a todo y los tests seguirían verdes."""
        _sembrar_tanqueos(mundo, llenos=7, dias_entre=15)
        r = _turno(client, mundo)['rendimiento']
        assert r['publicable'] is True, r['motivo']
        assert r['km_galon'] != 'sin_dato'
        assert r['ventanas'] == 6 and r['dias_historia'] >= 60

    def test_con_CINCO_ventanas_todavia_NO(self, client, db, mundo):
        """Justo por debajo. Es lo que mata la mutación que baja el umbral: con
        cinco ventanas el número existe y aun así no se muestra."""
        _sembrar_tanqueos(mundo, llenos=6, dias_entre=20)   # 5 ventanas, 100 días
        r = _turno(client, mundo)['rendimiento']
        assert r['ventanas'] == 5
        assert r['publicable'] is False
        assert '5 ventana(s) de 6' in r['motivo']
        assert r['km_galon'] == 'sin_dato', (
            'el número viajó a la pantalla del conductor sin poder sostenerse')

    def test_con_SEIS_ventanas_pero_POCOS_DIAS_todavia_NO(
            self, client, db, mundo):
        """El otro umbral, aislado. Seis ventanas en tres semanas son seis
        tanqueos de la misma ruta, no seis mediciones — y sin este test, borrar
        la condición de días no rompe nada."""
        _sembrar_tanqueos(mundo, llenos=7, dias_entre=3)    # 6 ventanas, 18 días
        r = _turno(client, mundo)['rendimiento']
        assert r['ventanas'] == 6 and r['dias_historia'] < 60
        assert r['publicable'] is False
        assert 'día(s) de historia' in r['motivo']
        assert r['km_galon'] == 'sin_dato'

    def test_los_tanqueos_PARCIALES_de_los_extremos_se_cuentan_fuera(
            self, client, db, mundo):
        """La condición 2, con un número distinto de cero.

        El test viejo solo comprobaba que la clave existiera, así que devolver
        siempre 0 lo pasaba — y un rendimiento sobre 3 de 20 tanqueos se vería
        idéntico a uno sobre 19 de 20.
        """
        from datetime import date as _date

        from flota.adaptadores import gastos as adaptador

        # Un parcial ANTES del primer lleno: no entra a ninguna ventana.
        adaptador.registrar_tanqueo(
            vehiculo_id=mundo['veh'], fecha=_date(2026, 1, 1), valor='50000',
            galones='4', tanque='parcial', estacion='T', km=9000,
            proveedor='T', origen_costo='tarjeta_convenio',
            registrado_por_usuario_id=mundo['usr'],
            ts=datetime(2026, 1, 1, 8, 0))
        _sembrar_tanqueos(mundo, llenos=7, dias_entre=15)

        r = _turno(client, mundo)['rendimiento']
        assert r['tanqueos_fuera_por_parcial'] == 1, (
            'el tanqueo que no entró a ninguna ventana no se contó: un '
            'rendimiento sobre 3 de 20 se vería igual que uno sobre 19 de 20')
        assert r['publicable'] is True
