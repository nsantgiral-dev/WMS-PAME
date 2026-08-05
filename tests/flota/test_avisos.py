"""
El aviso de vencimiento: lo único de las tres plantillas que tiene de qué avisar.

Al 2026-08-05 las tres plantillas están creadas en WhatsApp, pero **dos no
tienen fuente de datos**: no existe la tabla `flota_hallazgo`, ni un endpoint
para crear hallazgos. `flota_hallazgo_bloqueante` y `flota_hallazgo_vencido`
esperan a la tanda 2. `flota_documento_vence` sí puede correr hoy, porque
`DocumentoVehiculo` existe y se está cargando.

Lo que estos tests protegen, en orden de lo que cuesta:

1. **Que nadie reciba un mensaje mal escrito.** Una plantilla de WhatsApp no
   tiene condicionales: no concuerda número ni género ni formatea fechas. Todo
   llega resuelto en la variable o sale mal, y sale mal SIN error.
2. **Que `submitted` no se lea como `entregado`.** El módulo existe para que un
   vencimiento no se quede quieto; un aviso que no llegó y figura como enviado
   es el fallo exacto que hay que poder ver.
3. **Que el doble se distinga del real** en la fila, no solo en el log.
4. **Que no se repita.** Tres avisos iguales silencian el chat, y entonces el
   que importa llega a un silencio.
"""
import json
from datetime import date, timedelta

import pytest

from flota.dominio.aviso import (
    AvisoInvalido,
    clave_aviso,
    dias_en_palabras,
    fecha_en_palabras,
    nombre_documento,
    parametros_documento_vence,
    toca_avisar_vencimiento,
    validar_parametros,
)


class TestLasTresTrampasDeRedaccion:
    """Documentadas al enviar las plantillas a aprobación, el 2026-08-03."""

    def test_un_dia_no_dice_1_dias(self):
        """`{{3}}` recibe "3 días", no 3. Con el entero, el valor 1 produce
        "venció hace 1 días" — y lo lee una persona."""
        assert dias_en_palabras(1) == '1 día'
        assert dias_en_palabras(3) == '3 días'
        assert dias_en_palabras(0) == '0 días'

    def test_la_fecha_va_en_palabras(self):
        """Ni ISO ni numérico. El ISO se lee como un error del sistema —es el
        `2026-07-15` que salió a producción en cartera— y 15/08 es ambiguo."""
        assert fecha_en_palabras(date(2026, 8, 15)) == '15 de agosto de 2026'
        assert fecha_en_palabras(date(2026, 1, 1)) == '1 de enero de 2026'
        assert fecha_en_palabras(date(2026, 12, 31)) == '31 de diciembre de 2026'

    def test_el_documento_se_nombra_como_lo_nombra_la_gente(self):
        assert nombre_documento('soat') == 'SOAT'
        assert nombre_documento('rtm') == 'tecnomecánica'

    def test_un_tipo_desconocido_no_se_manda_con_guion_bajo(self):
        """Sin `.get(tipo, tipo)`: mandaría `poliza_rc` a un WhatsApp que
        alguien lee. Regla 5 — o funciona, o falla."""
        with pytest.raises(AvisoInvalido, match='desconocido'):
            nombre_documento('licencia_transito')

    def test_el_orden_de_los_parametros_es_el_de_la_plantilla(self):
        """Son posicionales. Reordenarlos manda la fecha donde va la placa y
        Gupshup responde exactamente igual de bien."""
        p = parametros_documento_vence('tgz653', 'soat', date(2026, 8, 15))
        assert p == ['TGZ653', 'SOAT', '15 de agosto de 2026']


class TestUnMensajeNoSaleAMedias:
    """WhatsApp manda un parámetro vacío sin quejarse. El hueco lo ve la persona."""

    def test_la_cantidad_de_parametros_se_verifica(self):
        with pytest.raises(AvisoInvalido, match='posicionales'):
            validar_parametros('flota_documento_vence', ['TGZ653', 'SOAT'])

    def test_una_plantilla_que_no_existe_no_se_manda(self):
        with pytest.raises(AvisoInvalido, match='desconocida'):
            validar_parametros('flota_lo_que_sea', ['a'])

    def test_un_parametro_vacio_se_rechaza(self):
        with pytest.raises(AvisoInvalido):
            validar_parametros('flota_documento_vence', ['TGZ653', '', 'hoy'])

    def test_el_texto_None_se_rechaza(self):
        """`str(None)` es `'None'`, mide 4 y es truthy. Es el guard de forma que
        ya costó una vez, aplicado al cuerpo del mensaje."""
        with pytest.raises(AvisoInvalido, match='dato ausente'):
            validar_parametros('flota_documento_vence', ['TGZ653', 'None', 'hoy'])

    def test_sin_dato_tampoco_viaja(self):
        with pytest.raises(AvisoInvalido):
            validar_parametros('flota_documento_vence', ['TGZ653', 'sin_dato', 'hoy'])

    def test_las_tres_plantillas_declaran_cuantos_parametros_llevan(self):
        from flota.dominio.aviso import PLANTILLAS

        assert PLANTILLAS == {
            'flota_documento_vence': 3,
            'flota_hallazgo_bloqueante': 2,
            'flota_hallazgo_vencido': 3,
        }


class TestCuandoSeAvisa:

    def test_es_una_ventana_y_no_un_dia_exacto(self):
        """Con `dias_restantes == 15`, un día que el cron no corra —deploy,
        caída, variable apagada— se salta el aviso PARA SIEMPRE."""
        hoy = date(2026, 8, 1)
        assert toca_avisar_vencimiento(date(2026, 8, 16), hoy) is True   # 15
        assert toca_avisar_vencimiento(date(2026, 8, 10), hoy) is True   # 9
        assert toca_avisar_vencimiento(date(2026, 8, 1), hoy) is True    # hoy
        assert toca_avisar_vencimiento(date(2026, 8, 20), hoy) is False  # 19

    def test_un_documento_ya_vencido_no_entra(self):
        """No es un aviso de renovación: es un vehículo que no debería salir, y
        se atiende por otra vía."""
        assert toca_avisar_vencimiento(date(2026, 7, 31), date(2026, 8, 1)) is False

    def test_sin_fecha_no_se_avisa(self):
        assert toca_avisar_vencimiento(None, date(2026, 8, 1)) is False

    def test_la_clave_lleva_el_hito_para_que_una_renovacion_vuelva_a_avisar(self):
        a = clave_aviso('flota_documento_vence', 'documento', 7, '2026-08-15')
        b = clave_aviso('flota_documento_vence', 'documento', 7, '2027-08-15')
        assert a != b


class TestElIdDePlantillaNoEsSuNombre:
    """El bug que costó semanas en cartera, entero.

    `_TEMPLATE_IDS.get(nombre, nombre)` mandaba el nombre como id: Gupshup
    respondía `submitted` y no llegaba nada. No hay error, no hay log, no hay
    forma de notarlo salvo que alguien pregunte por qué no le llegó.
    """

    def test_sin_id_configurado_levanta_en_vez_de_usar_el_nombre(self, monkeypatch):
        from flota.adaptadores.gupshup import AvisoNoEnviado, id_de_plantilla

        monkeypatch.delenv('GUPSHUP_TEMPLATE_IDS', raising=False)
        with pytest.raises(AvisoNoEnviado, match='no hay id de Gupshup'):
            id_de_plantilla('flota_documento_vence')

    def test_el_mensaje_avisa_de_las_dos_confusiones_conocidas(self, monkeypatch):
        """El id temporal (mientras estaba Pending) y el de Facebook. Las dos
        producen el mismo síntoma: aceptado y no entregado."""
        from flota.adaptadores.gupshup import AvisoNoEnviado, id_de_plantilla

        monkeypatch.delenv('GUPSHUP_TEMPLATE_IDS', raising=False)
        with pytest.raises(AvisoNoEnviado) as e:
            id_de_plantilla('flota_documento_vence')
        assert 'temporal' in str(e.value) and 'Facebook' in str(e.value)

    def test_un_id_de_facebook_se_rechaza_por_su_forma(self, monkeypatch):
        """El de Facebook es todo dígitos; el de Gupshup es un uuid. Se
        distinguen sin preguntarle a nadie."""
        from flota.adaptadores.gupshup import AvisoNoEnviado, id_de_plantilla

        monkeypatch.setenv('GUPSHUP_TEMPLATE_IDS',
                           '{"flota_documento_vence": "1755955632267884"}')
        with pytest.raises(AvisoNoEnviado, match='id de Facebook'):
            id_de_plantilla('flota_documento_vence')

    def test_un_uuid_de_gupshup_pasa(self, monkeypatch):
        from flota.adaptadores.gupshup import id_de_plantilla

        monkeypatch.setenv(
            'GUPSHUP_TEMPLATE_IDS',
            '{"flota_documento_vence": "bd442d44-8308-4d26-bb1c-c2d038811893"}')
        assert id_de_plantilla('flota_documento_vence').startswith('bd442d44')

    def test_un_json_roto_no_degrada_a_mapa_vacio_silencioso(self, monkeypatch):
        from flota.adaptadores.gupshup import AvisoNoEnviado, id_de_plantilla

        monkeypatch.setenv('GUPSHUP_TEMPLATE_IDS', '{esto no es json}')
        with pytest.raises(AvisoNoEnviado, match='JSON'):
            id_de_plantilla('flota_documento_vence')


class TestElGuardDeTelefonoVerificaForma:

    def test_el_texto_None_no_pasa(self):
        """`str(None)` es truthy: un `if telefono:` lo deja pasar."""
        from flota.adaptadores.gupshup import AvisoNoEnviado, validar_telefono

        with pytest.raises(AvisoNoEnviado):
            validar_telefono(str(None))

    def test_None_tampoco(self):
        from flota.adaptadores.gupshup import AvisoNoEnviado, validar_telefono

        with pytest.raises(AvisoNoEnviado):
            validar_telefono(None)

    def test_normaliza_lo_que_la_gente_escribe(self):
        from flota.adaptadores.gupshup import validar_telefono

        assert validar_telefono('+57 300 111 2233') == '573001112233'
        assert validar_telefono('573001112233') == '573001112233'

    def test_un_numero_corto_no_pasa(self):
        from flota.adaptadores.gupshup import AvisoNoEnviado, validar_telefono

        with pytest.raises(AvisoNoEnviado):
            validar_telefono('3001')


class TestNaceApagado:
    """Regla 10: todo cron que escribe nace apagado, por variable de entorno."""

    def test_sin_la_variable_el_barrido_no_corre(self, app, db, monkeypatch):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        monkeypatch.delenv('FLOTA_AVISOS', raising=False)
        r = barrer_documentos_por_vencer()
        assert r['enviados'] == 0
        assert 'FLOTA_AVISOS' in r['motivo']

    def test_encendido_el_canal_por_defecto_sigue_siendo_el_simulado(self, monkeypatch):
        """Dos decisiones separadas: encender el barrido y mandar de verdad.
        Un cron que nace mandando WhatsApp a tres empleados el día que alguien
        lo despliega sin querer es la regla 10 al revés."""
        from flota.adaptadores.gupshup import CanalSimulado, canal

        monkeypatch.delenv('FLOTA_AVISOS_REALES', raising=False)
        assert isinstance(canal(), CanalSimulado)

    def test_el_canal_real_exige_decirlo_explicitamente(self, monkeypatch):
        from flota.adaptadores.gupshup import CanalGupshup, canal

        monkeypatch.setenv('FLOTA_AVISOS_REALES', 'true')
        assert isinstance(canal(), CanalGupshup)


def _documento(db, dias, tipo='soat', placa='AVI100'):
    from app.models.vehiculo import Vehiculo
    from app.utils.fecha import dia_operativo
    from flota.adaptadores.modelos import DocumentoVehiculo

    v = Vehiculo.query.filter_by(placa=placa).first()
    if v is None:
        v = Vehiculo(placa=placa, tipo='NHR', activo=True)
        db.session.add(v)
        db.session.flush()
    d = DocumentoVehiculo(
        vehiculo_id=v.id, tipo=tipo, numero='S-1', entidad='Aseguradora',
        fecha_expedicion=dia_operativo() - timedelta(days=300),
        fecha_vencimiento=dia_operativo() + timedelta(days=dias),
        estado='vigente',
    )
    db.session.add(d)
    db.session.commit()
    return d


@pytest.fixture
def encendido(monkeypatch):
    monkeypatch.setenv('FLOTA_AVISOS', 'true')
    monkeypatch.setenv('FLOTA_AVISO_TELEFONOS',
                       json.dumps({'mantenimiento': ['573001112233']}))


class TestElBarrido:

    def _canal(self):
        from flota.adaptadores.gupshup import CanalSimulado

        return CanalSimulado()

    def test_avisa_por_un_soat_que_vence_en_diez_dias(self, app, db, encendido):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        _documento(db, 10)
        c = self._canal()
        r = barrer_documentos_por_vencer(canal_usado=c)

        assert r['en_ventana'] == 1 and r['enviados'] == 1
        destino, plantilla, params = c.enviados[0]
        assert plantilla == 'flota_documento_vence'
        assert params[0] == 'AVI100' and params[1] == 'SOAT'
        assert ' de ' in params[2], 'la fecha tiene que ir en palabras'

    def test_no_avisa_por_uno_que_vence_en_sesenta_dias(self, app, db, encendido):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        _documento(db, 60)
        r = barrer_documentos_por_vencer(canal_usado=self._canal())
        assert r['enviados'] == 0

    def test_correr_dos_veces_NO_avisa_dos_veces(self, app, db, encendido):
        """Si el cron reenviara cada noche, en tres días el chat se silencia —
        y entonces el aviso que importa llega a un silencio."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        _documento(db, 10)
        barrer_documentos_por_vencer(canal_usado=self._canal())
        c2 = self._canal()
        r = barrer_documentos_por_vencer(canal_usado=c2)

        assert r['enviados'] == 0 and r['ya_avisados'] == 1
        assert c2.enviados == []

    def test_un_documento_RENOVADO_vuelve_a_avisar(self, app, db, encendido):
        """La idempotencia no puede convertirse en silencio permanente: el SOAT
        del año que viene es otro evento."""
        from app.utils.fecha import dia_operativo
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        doc = _documento(db, 10)
        barrer_documentos_por_vencer(canal_usado=self._canal())

        doc.fecha_vencimiento = dia_operativo() + timedelta(days=375)
        db.session.commit()
        # Un año después vuelve a entrar en ventana:
        r = barrer_documentos_por_vencer(
            canal_usado=self._canal(),
            hoy=dia_operativo() + timedelta(days=365))
        assert r['enviados'] == 1

    def test_sin_telefonos_no_inventa_destinatario_y_lo_cuenta(self, app, db, monkeypatch):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        monkeypatch.setenv('FLOTA_AVISOS', 'true')
        monkeypatch.delenv('FLOTA_AVISO_TELEFONOS', raising=False)
        _documento(db, 10)
        r = barrer_documentos_por_vencer(canal_usado=self._canal())
        assert r['enviados'] == 0 and r['sin_destinatario'] == 1

    def test_un_documento_no_encontrado_no_dispara_aviso_de_renovacion(
            self, app, db, encendido):
        from flota.adaptadores.modelos import DocumentoVehiculo
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        doc = _documento(db, 10)
        doc.estado = 'no_encontrado'
        doc.fecha_expedicion = doc.fecha_vencimiento = None
        db.session.commit()
        r = barrer_documentos_por_vencer(canal_usado=self._canal())
        assert r['enviados'] == 0

    def test_el_barrido_dice_que_hizo(self, app, db, encendido):
        """Un barrido que no reporta es indistinguible de uno que no corrió."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        _documento(db, 10)
        r = barrer_documentos_por_vencer(canal_usado=self._canal())
        for k in ('revisados', 'en_ventana', 'enviados', 'ya_avisados',
                  'fallidos', 'sin_destinatario', 'simulado'):
            assert k in r


class TestSubmittedNoEsDelivered:
    """El estado que hace honesto a todo lo demás."""

    def _sembrar(self, db, encendido_fixture=None):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.gupshup import CanalSimulado
        from flota.adaptadores.modelos import Aviso

        _documento(db, 10)
        barrer_documentos_por_vencer(canal_usado=CanalSimulado())
        return Aviso.query.one()

    def test_lo_aceptado_por_el_proveedor_NO_figura_como_entregado(
            self, app, db, encendido):
        fila = self._sembrar(db)
        assert fila.estado == 'entregado_al_proveedor', (
            'Gupshup dijo "lo recibí", no "llegó" — y el tablero de cartera que '
            'confundía las dos cosas costó semanas')

    def test_el_evento_de_entrega_lo_mueve_a_entregado(self, app, db, encendido):
        from flota.adaptadores.avisos import registrar_entrega

        fila = self._sembrar(db)
        assert registrar_entrega(fila.proveedor_msg_id, 'delivered') is True
        db.session.refresh(fila)
        assert fila.estado == 'entregado' and fila.entregado_ts is not None

    def test_un_evento_desordenado_no_hace_retroceder_el_estado(
            self, app, db, encendido):
        """Los eventos llegan cuando llegan. Un tablero que baja de estado se
        lee como un problema que no existe."""
        from flota.adaptadores.avisos import registrar_entrega

        fila = self._sembrar(db)
        registrar_entrega(fila.proveedor_msg_id, 'read')
        registrar_entrega(fila.proveedor_msg_id, 'delivered')
        db.session.refresh(fila)
        assert fila.estado == 'leido'

    def test_un_fallo_reportado_si_manda(self, app, db, encendido):
        from flota.adaptadores.avisos import registrar_entrega

        fila = self._sembrar(db)
        registrar_entrega(fila.proveedor_msg_id, 'failed')
        db.session.refresh(fila)
        assert fila.estado == 'fallido'

    def test_un_evento_sin_fila_no_revienta(self, app, db):
        from flota.adaptadores.avisos import registrar_entrega

        assert registrar_entrega('id-que-no-existe', 'delivered') is False

    def test_los_que_nunca_confirmaron_se_cuentan(self, app, db, encendido):
        """El número que descubre el modo de fallo real: el canal acepta
        mensajes que no llegan. Un contador de "enviados" no lo puede ver."""
        from datetime import datetime, timedelta as _td

        from flota.adaptadores.avisos import avisos_sin_confirmar

        fila = self._sembrar(db)
        fila.simulado = False
        fila.creado_ts = datetime.utcnow() - _td(hours=9)
        db.session.commit()
        assert avisos_sin_confirmar(6) == 1

    def test_los_simulados_no_cuentan_como_sin_confirmar(self, app, db, encendido):
        """Nunca van a confirmar: no salieron. Contarlos convertiría el
        indicador en ruido permanente."""
        from datetime import datetime, timedelta as _td

        from flota.adaptadores.avisos import avisos_sin_confirmar

        fila = self._sembrar(db)
        fila.creado_ts = datetime.utcnow() - _td(hours=9)
        db.session.commit()
        assert avisos_sin_confirmar(6) == 0


class TestElDobleSeDistingueDelReal:
    """Regla 8. `CanalNotificacionDev` costó una hora de creer que 1.485
    personas habían recibido un cobro que nunca salió."""

    def test_la_FILA_dice_que_fue_simulado(self, app, db, encendido):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.gupshup import CanalSimulado
        from flota.adaptadores.modelos import Aviso

        _documento(db, 10)
        barrer_documentos_por_vencer(canal_usado=CanalSimulado())
        assert Aviso.query.one().simulado is True

    def test_el_id_simulado_no_se_parece_a_uno_real(self):
        from flota.adaptadores.gupshup import CanalSimulado

        c = CanalSimulado()
        msg_id = c.enviar('573001112233', 'flota_documento_vence',
                          ['TGZ653', 'SOAT', '15 de agosto de 2026'])
        assert msg_id.startswith('SIMULADO-')

    def test_el_doble_valida_lo_mismo_que_el_real(self):
        """Un doble más permisivo que el real deja pasar en pruebas lo que
        revienta en producción."""
        from flota.adaptadores.gupshup import CanalSimulado

        with pytest.raises(AvisoInvalido):
            CanalSimulado().enviar('573001112233', 'flota_documento_vence',
                                   ['TGZ653', 'SOAT'])


class TestElWebhookNaceCerrado:

    def test_sin_token_configurado_no_acepta_nada(self, client, monkeypatch):
        """Un webhook abierto es un endpoint por el que cualquiera declara
        entregado lo que nunca llegó."""
        monkeypatch.delenv('FLOTA_AVISO_WEBHOOK_TOKEN', raising=False)
        r = client.post('/flota/avisos/entrega', json={})
        assert r.status_code == 503

    def test_con_token_equivocado_rechaza(self, client, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        r = client.post('/flota/avisos/entrega?token=otro', json={})
        assert r.status_code == 403

    def test_un_evento_que_no_cruza_devuelve_200_y_no_error(
            self, app, db, client, monkeypatch):
        """Un webhook que responde error hace que el proveedor reintente en
        bucle. Lo que no se pudo cruzar va al log, no a un 500."""
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        r = client.post('/flota/avisos/entrega?token=secreto',
                        json={'payload': {'gsId': 'nada', 'type': 'delivered'}})
        assert r.status_code == 200
        assert r.get_json()['cruzado'] is False


class TestLaDeduplicacionConsultaLoQueEscribe:
    """El bug que encontró el test de "correr dos veces".

    La clave se construía sin sufijo para consultar y CON sufijo para escribir.
    La consulta nunca encontraba nada, así que el segundo barrido no se saltaba
    el aviso: reventaba contra el índice único. Con una sola línea de
    `except Exception` en el cron, eso habría sido un barrido que deja de
    funcionar en silencio desde el segundo día.
    """

    def test_la_clave_escrita_es_la_que_se_consulta(self, app, db, encendido):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.gupshup import CanalSimulado
        from flota.adaptadores.modelos import Aviso

        doc = _documento(db, 10)
        barrer_documentos_por_vencer(canal_usado=CanalSimulado())

        escrita = Aviso.query.one().clave
        base = clave_aviso('flota_documento_vence', 'documento', doc.id,
                           doc.fecha_vencimiento.isoformat())
        assert escrita == f'{base}:0'

    def test_un_destinatario_agregado_despues_SI_recibe(self, app, db, monkeypatch):
        """La idempotencia es por persona, no por documento: quien se suma
        mañana no puede quedar afuera porque "ya se avisó" a otro."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.gupshup import CanalSimulado

        monkeypatch.setenv('FLOTA_AVISOS', 'true')
        monkeypatch.setenv('FLOTA_AVISO_TELEFONOS',
                           json.dumps({'mantenimiento': ['573001112233']}))
        _documento(db, 10)
        barrer_documentos_por_vencer(canal_usado=CanalSimulado())

        monkeypatch.setenv('FLOTA_AVISO_TELEFONOS',
                           json.dumps({'mantenimiento': ['573001112233',
                                                         '573009998877']}))
        c = CanalSimulado()
        r = barrer_documentos_por_vencer(canal_usado=c)
        assert r['enviados'] == 1
        assert c.enviados[0][0] == '573009998877'

    def test_dos_destinatarios_reciben_los_dos(self, app, db, monkeypatch):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.gupshup import CanalSimulado

        monkeypatch.setenv('FLOTA_AVISOS', 'true')
        monkeypatch.setenv('FLOTA_AVISO_TELEFONOS',
                           json.dumps({'mantenimiento': ['573001112233',
                                                         '573009998877']}))
        _documento(db, 10)
        c = CanalSimulado()
        assert barrer_documentos_por_vencer(canal_usado=c)['enviados'] == 2
        assert len({t for t, _, _ in c.enviados}) == 2
