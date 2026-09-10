"""
El reporte de tres líneas de los lunes — el que llega armado o no llega.

Especificado en `docs/flota/ESTADO.md:536-548` el 2026-08-01, con **cero líneas
de código** hasta el 2026-09-04. Lo que la spec dice y este archivo protege:

> **Si Yesid tiene que construirlo, no lo va a construir.** El sistema trabaja
> para él, no al revés — esa es la diferencia entre un rol de 30 minutos y uno
> que nadie sostiene.

## Las tres formas de que esto falle sin que nadie se entere

1. **Corre el lunes y pregunta por la semana que empezó hace tres horas.** Un
   correo de ceros todos los lunes, que se lee como «no pasó nada».
2. **Se manda a quien no es.** `enviar_email` tomaba los destinatarios de
   `ALERTA_EMAIL_DEST`, cuyo destinatario documentado es el Jefe de Bodega:
   status 200, log de «enviado», y el tablero de flota en la bandeja
   equivocada durante meses.
3. **Nunca arranca.** El scheduler va en `_scheduler_pesados`, detrás de
   `HEAVY_SCHEDULERS=true`, y una alerta apagada no falla: se calla.

Las tres se ven exactamente igual desde adentro del sistema.
"""
from datetime import date, datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from app.models.usuario import Usuario
from app.models.vehiculo import Vehiculo
from flota.adaptadores import reporte_semanal as rep


@pytest.fixture
def mundo(db, almacen):
    v = Vehiculo(placa='RPT100', tipo='NHR', activo=True)
    u = Usuario(nombre='Control RPT', email='rpt@t.co', rol='control_flota',
                almacen_id=almacen.id, activo=True,
                password_hash=generate_password_hash('x'))
    db.session.add_all([v, u])
    db.session.commit()
    # Regla 3: ningún evento de flota se persiste sin odómetro. La lectura se
    # ancla con la misma política que usan los gastos y los hallazgos, no con
    # una fila fabricada acá.
    from flota.adaptadores.hallazgos import anclar_odometro
    from datetime import datetime as _dt
    lec = anclar_odometro(vehiculo_id=v.id, km=1000, autor_usuario_id=u.id,
                          ahora=_dt(2026, 9, 1, 8, 0))
    db.session.commit()
    return {'veh': v.id, 'placa': v.placa, 'usr': u.id, 'db': db,
            'lectura': lec.id,
            't': create_access_token(identity=str(u.id))}


class TestLaVentanaEsLaSemanaCERRADA:
    """El defecto que el nombre de la función evita, medido sobre el reporte.

    Ese fallo no revienta y no deja rastro: es la misma forma que la lectura
    temprana que dejó 28 requisiciones huérfanas en Siesa.
    """

    def test_corrido_un_LUNES_pregunta_por_la_semana_anterior(self, app, db, mundo):
        r = rep.armar_reporte(dia=date(2026, 9, 7))     # lunes
        assert (r['desde'], r['hasta']) == ('2026-08-31', '2026-09-06')

    def test_la_ventana_NUNCA_incluye_el_dia_en_que_corre(self, app, db, mundo):
        """La propiedad, no el ejemplo. Si el día cae dentro de su propia
        semana «cerrada», la semana no estaba cerrada."""
        for salto in range(14):
            dia = date(2026, 9, 1) + timedelta(days=salto)
            r = rep.armar_reporte(dia=dia)
            assert r['hasta'] < dia.isoformat(), f'{dia} cae en su propia ventana'

    def test_declara_su_ventana_y_su_dia_de_calculo(self, app, db, mundo):
        """Regla 13 en un correo: tres números sin decir de cuándo son se leen
        como los de esta semana."""
        r = rep.armar_reporte(dia=date(2026, 9, 7))
        texto = rep._texto(r)
        assert '2026-08-31' in texto and '2026-09-06' in texto
        assert 'día operativo de Bogotá' in texto


class TestLasTresLineas:

    def test_las_incompletas_van_APARTE_y_no_se_suman(self, app, db, mundo):
        """Una inspección incompleta no es una hecha a medias que cuente un
        poco: es un «no sé» (regla 1) y no habilita despacho. Un solo total
        escondería cuántos camiones salieron sin que nadie pudiera decir si
        estaban bien."""
        from flota.adaptadores.modelos import (Inspeccion,
                                               PlantillaInspeccion)
        from flota.dominio import inspeccion as dom_insp

        # `plantilla_id` es NOT NULL: una inspección sin catálogo no dice contra
        # qué lista se respondió. Se siembra la plantilla que el tipo del
        # vehículo pide, con el mismo mapa que usa el adaptador — no un id
        # inventado, que probaría que la FK acepta un número.
        plantilla = PlantillaInspeccion(
            codigo='RPT-TEST', nombre='Plantilla de prueba', version=1, activa=True,
            aplica_a=dom_insp.plantilla_de_tipo('NHR'))
        db.session.add(plantilla)
        db.session.commit()

        # `items_sin_dato` acompaña al veredicto y no es decoración:
        # `ck_flota_insp_veredicto_coherente` exige que una `incompleta` tenga
        # ítems sin responder — que es literalmente lo que la palabra significa.
        # Una fila «incompleta» con cero huecos afirmaría dos cosas que no
        # pueden ser las dos ciertas.
        for veredicto, sin_dato in (('apto', 0), ('apto', 0), ('incompleta', 2)):
            db.session.add(Inspeccion(
                vehiculo_id=mundo['veh'], dia=date(2026, 9, 2),
                veredicto=veredicto,
                inspeccionada_por_usuario_id=mundo['usr'],
                plantilla_id=plantilla.id,
                lectura_id=mundo['lectura'], items_esperados=10,
                items_sin_dato=sin_dato, bloqueantes_no_aptos=0,
                segundos_llenado=120))
        db.session.commit()

        r = rep.armar_reporte(dia=date(2026, 9, 7))
        assert r['inspecciones']['completas'] == 2
        assert r['inspecciones']['incompletas'] == 1
        assert 'no habilita despacho' in rep._texto(r)

    def test_los_hallazgos_vencidos_traen_sus_DIAS_y_el_peor_primero(
            self, app, db, mundo):
        """Un listado de vencidos sin días no se puede triar. Y el orden
        importa: quien lee cinco minutos lee las primeras líneas."""
        from flota.adaptadores import hallazgos as adap

        # Por el adaptador y no insertando la fila: `lectura_id` es NOT NULL
        # —regla 3, sin odómetro no se persiste ningún evento de flota— y un
        # arnés que escribe la fila directo solo probaría que la base la acepta.
        ahora = datetime(2026, 9, 7, 12, 0)
        km = 1000
        for dias, crit in ((30, 'mayor'), (5, 'bloqueante')):
            h = adap.reportar(
                vehiculo_id=mundo['veh'], criticidad=crit,
                descripcion=f'daño de hace {dias} días', km=km,
                reportado_por_usuario_id=mundo['usr'],
                ts=ahora - timedelta(days=dias))
            # Se fuerza el vencimiento para no depender del plazo por
            # criticidad: lo que este test mide es el ORDEN y los días, no la
            # tabla de plazos —que tiene su propio test en el canon.
            h.fecha_limite = ahora - timedelta(days=1)
            h.linea_base = False
            km += 100
        db.session.commit()

        r = rep.armar_reporte(dia=date(2026, 9, 7), ahora=ahora)
        vencidos = r['hallazgos_vencidos']
        assert [h['dias'] for h in vencidos] == [30, 5], 'no salió el peor primero'
        assert all(h['placa'] == mundo['placa'] for h in vencidos)

    def test_los_documentos_traen_CUANTOS_DIAS_FALTAN(self, app, db, mundo):
        """«Vence el 2026-09-28» obliga a restar; «faltan 24 días» no. El
        correo lo lee alguien en cinco minutos."""
        from flota.adaptadores.modelos import DocumentoVehiculo

        # `estado='vigente'` exige expedición, número y entidad
        # (`ck_flota_doc_estado_coherente`): un documento «vigente» sin datos
        # sería una fila que afirma más de lo que sabe.
        db.session.add(DocumentoVehiculo(
            vehiculo_id=mundo['veh'], tipo='rtm', numero='RTM-1',
            entidad='CDA Neiva', fecha_expedicion=date(2025, 10, 1),
            fecha_vencimiento=date(2026, 10, 1), estado='vigente'))
        db.session.commit()

        r = rep.armar_reporte(dia=date(2026, 9, 7))
        docs = r['documentos_por_vencer_30d']
        assert len(docs) == 1 and docs[0]['faltan'] == 24

    def test_un_documento_a_MAS_de_30_dias_no_entra(self, app, db, mundo):
        """La otra dirección. Sin esto, «los que vencen en 30 días» podría ser
        «todos» y el test seguiría verde."""
        from flota.adaptadores.modelos import DocumentoVehiculo

        db.session.add(DocumentoVehiculo(
            vehiculo_id=mundo['veh'], tipo='soat', numero='SOAT-1',
            entidad='Sura', fecha_expedicion=date(2025, 12, 1),
            fecha_vencimiento=date(2026, 12, 1), estado='vigente'))
        db.session.commit()

        assert rep.armar_reporte(dia=date(2026, 9, 7))['documentos_por_vencer_30d'] == []


class TestNaceApagadoYNoManda:
    """Regla 10, y la comprobación NO es sobre el valor de retorno.

    Un test que solo mire el dict pasa con el correo saliendo. Lo que se cuenta
    son las llamadas a Resend.
    """

    def test_sin_la_variable_no_llama_a_resend(self, app, db, mundo, monkeypatch):
        monkeypatch.delenv('FLOTA_REPORTE_SEMANAL', raising=False)
        llamadas = []
        monkeypatch.setattr('app.services.alertas_service.enviar_email',
                            lambda *a, **k: llamadas.append(a))

        r = rep.enviar_reporte_semanal(dia=date(2026, 9, 7))
        assert r['enviado'] is False
        assert 'FLOTA_REPORTE_SEMANAL' in r['motivo']
        assert llamadas == [], 'mandó el correo con el interruptor apagado'

    def test_encendido_pero_SIN_DESTINATARIOS_tampoco_manda(
            self, app, db, mundo, monkeypatch):
        """**No cae a `ALERTA_EMAIL_DEST`.**

        Caer a la lista global sería un adaptador degradando hacia algo que se
        parece al éxito: status 200, log de «enviado», y el tablero de flota
        llegándole meses al Jefe de Bodega — que no tiene ninguna decisión de
        flota asignada.
        """
        monkeypatch.setenv('FLOTA_REPORTE_SEMANAL', 'true')
        monkeypatch.delenv('FLOTA_REPORTE_DEST', raising=False)
        monkeypatch.setenv('ALERTA_EMAIL_DEST', 'jefe.bodega@pame.co')
        llamadas = []
        monkeypatch.setattr('app.services.alertas_service.enviar_email',
                            lambda *a, **k: llamadas.append(a))

        r = rep.enviar_reporte_semanal(dia=date(2026, 9, 7))
        assert r['enviado'] is False
        assert 'FLOTA_REPORTE_DEST' in r['motivo']
        assert llamadas == [], 'heredó la lista global de alertas'

    def test_con_las_DOS_variables_si_manda_a_SUS_destinatarios(
            self, app, db, mundo, monkeypatch):
        """La dirección que faltaba: sin esto, «no mandó» podría ser la
        respuesta a todo y los dos tests de arriba seguirían verdes."""
        monkeypatch.setenv('FLOTA_REPORTE_SEMANAL', 'true')
        monkeypatch.setenv('FLOTA_REPORTE_DEST', 'yesid@pame.co,santiago@pame.co')
        monkeypatch.setenv('ALERTA_EMAIL_DEST', 'jefe.bodega@pame.co')
        recibido = {}

        def _falso(asunto, html, texto, dest=None):
            recibido.update(asunto=asunto, texto=texto, dest=dest)
            return True

        monkeypatch.setattr('app.services.alertas_service.enviar_email', _falso)

        r = rep.enviar_reporte_semanal(dia=date(2026, 9, 7))
        assert r['enviado'] is True
        assert recibido['dest'] == 'yesid@pame.co,santiago@pame.co'
        assert 'jefe.bodega' not in (recibido['dest'] or '')
        assert 'Flota · semana del 2026-08-31' in recibido['asunto']

    def test_armar_reporte_funciona_CON_EL_CRON_APAGADO(
            self, app, db, mundo, monkeypatch):
        """La mitigación del riesgo de `HEAVY_SCHEDULERS`: el contenido se puede
        verificar sin encender nada. Un cron que nunca arranca no puede estar
        escondiendo un reporte roto si alguien pudo mirarlo antes."""
        monkeypatch.delenv('FLOTA_REPORTE_SEMANAL', raising=False)
        r = rep.armar_reporte(dia=date(2026, 9, 7))
        assert r['desde'] and r['hasta'] and 'inspecciones' in r


class TestElSchedulerDeclara:

    def test_apagado_devuelve_None_y_no_arranca(self, app, monkeypatch):
        monkeypatch.delenv('FLOTA_REPORTE_SEMANAL', raising=False)
        assert rep.init_scheduler(app) is None

    def test_encendido_devuelve_el_scheduler(self, app, monkeypatch):
        """`app/__init__.py:56-59` declara «omitido» todo `init_scheduler` que
        devuelva falsy. Uno que arranca y no devuelve nada se reporta como no
        arrancado — y `/api/health/siesa` publicaría un estado falso."""
        monkeypatch.setenv('FLOTA_REPORTE_SEMANAL', 'true')
        sch = rep.init_scheduler(app)
        assert sch is not None
        try:
            job = sch.get_job('flota_reporte_semanal')
            assert job is not None
            assert 'mon' in str(job.trigger)
            # Al OBJETO y no a su `str()`: el repr de `CronTrigger` no incluye
            # el huso, así que un `'America/Bogota' in str(trigger)` sería un
            # test que nunca puede pasar — y uno que se «arregla» borrándolo.
            assert str(job.trigger.timezone) == 'America/Bogota'
        finally:
            sch.shutdown(wait=False)
