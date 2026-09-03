"""Un documento que ya venció no se queda en silencio.

## El hueco

`toca_avisar_vencimiento` excluye a los ya vencidos, y lo dice en su docstring:

    Un documento ya vencido NO entra acá: eso no es un aviso de renovación, es
    un vehículo que no debería salir, y **se atiende por otra vía**.

La exclusión es correcta —renovar antes y circular ilegal no son el mismo
mensaje, con la misma urgencia ni para la misma persona—. El problema es que
**esa otra vía no existía**: era el contador `documentos_vencidos` del health,
y `GET /flota/health` **no tiene un solo consumidor en todo el repo**.

Entonces el sistema funcionaba así: avisa quince días antes; si nadie renueva,
se calla **justo cuando el vehículo pasa a ser ilegal**, y el número que lo sabe
no lo mira nadie.

Medido el 2026-09-01 contra producción: hay una **RTM vencida desde
2025-11-11** que nunca generó un aviso y nunca lo iba a generar.

## Por qué el hito es la semana

Un vencido es una alarma que no se apaga sola — el camión sigue siendo ilegal
cada día hasta que alguien renueve. Las dos alternativas obvias son peores:

· **Avisar una sola vez** (hito = la fecha de vencimiento) es el silencio de
  hoy con un mensaje adelante. Si ese aviso se pierde, nadie vuelve a decir
  nada nunca.
· **Avisar todos los días** silencia el chat en tres días, y entonces el aviso
  que importa llega a un silencio. Es lo que `clave_aviso` ya declara como el
  motivo de que exista el hito.

Semanal es una **cadencia de notificación, no un umbral sobre el dato**: no
decide si algo está mal, decide cada cuánto se repite algo que ya se sabe que
está mal. Y se apaga sola: renovar cambia `fecha_vencimiento`, el documento
deja de estar vencido y el barrido no lo mira más.

## La plantilla, y la deuda que queda declarada

Se manda con `flota_documento_vence` y no con una propia. Una plantilla nueva
exige aprobación de Gupshup, y este canal **ya está bloqueado** esperando los
ids definitivos de ese mismo tercero: crear una segunda dependencia dejaría el
aviso de vencidos apagado más tiempo que el de por-vencer.

El mensaje sale correcto aunque en tiempo verbal incómodo. La **clave** sí lleva
`flota_documento_vencido` — no para separar la deduplicación (de eso ya se
encarga el hito: `2026-09-06` nunca puede igualar a `2026-W36`) sino para que el
registro diga de cuál de los dos avisos se trata. Sin eso, `flota_aviso` tendría
dos poblaciones distintas bajo el mismo nombre y no se podrían contar aparte.

Esa precisión salió de una mutación: cambiar el prefijo a `flota_documento_vence`
**no rompía ningún test**, porque la protección real es el hito. La afirmación
original de este docstring era imprecisa y el test que la respaldaba probaba
otra cosa.
"""
from datetime import date, timedelta

import pytest

from flota.dominio.aviso import hito_semanal, toca_avisar_vencido

_HOY = date(2026, 9, 1)


class TestLaReglaDeVencido:
    def test_un_documento_vencido_ayer_avisa(self):
        assert toca_avisar_vencido(_HOY - timedelta(days=1), _HOY) is True

    def test_uno_vencido_hace_meses_sigue_avisando(self):
        """**El caso real**: la RTM vencida el 2025-11-11. Una alarma que se
        apaga con el tiempo es peor que ninguna — el camión sigue ilegal."""
        assert toca_avisar_vencido(date(2025, 11, 11), _HOY) is True

    def test_uno_que_vence_HOY_no_es_un_vencido(self):
        """Hoy todavía es legal. Lo atiende el aviso de por-vencer, que es otro
        mensaje: renovar antes ≠ ya no podés salir."""
        assert toca_avisar_vencido(_HOY, _HOY) is False

    def test_uno_que_vence_mañana_tampoco(self):
        assert toca_avisar_vencido(_HOY + timedelta(days=1), _HOY) is False

    def test_sin_fecha_no_afirma_nada(self):
        """Un documento sin vencimiento no está vencido: no se sabe. Regla 4."""
        assert toca_avisar_vencido(None, _HOY) is False


class TestElHitoSemanal:
    def test_dos_dias_de_la_misma_semana_dan_el_mismo_hito(self):
        assert hito_semanal(date(2026, 9, 1)) == hito_semanal(date(2026, 9, 3))

    def test_semanas_distintas_dan_hitos_distintos(self):
        assert hito_semanal(date(2026, 9, 1)) != hito_semanal(date(2026, 9, 10))

    def test_cruza_el_año_sin_colisionar(self):
        """`isocalendar` puede dar semana 1 del año siguiente en diciembre. Sin
        el año en el hito, un vencido de fin de año dejaría de avisar por
        colisión con el enero anterior."""
        assert hito_semanal(date(2026, 12, 31)) != hito_semanal(date(2026, 1, 1))


class TestElBarridoAvisaDeLosVencidos:

    @pytest.fixture
    def mundo(self, db, monkeypatch):
        from app.models.vehiculo import Vehiculo
        monkeypatch.setenv('FLOTA_AVISOS', 'true')
        monkeypatch.setenv('FLOTA_AVISO_TELEFONOS',
                           '{"mantenimiento": ["573001112233"]}')
        v = Vehiculo(placa='VENC-01', tipo='NHR', activo=True)
        db.session.add(v)
        db.session.commit()
        return {'veh': v.id}

    @staticmethod
    def _doc(db, mundo, vencimiento, tipo='rtm'):
        from flota.adaptadores.modelos import DocumentoVehiculo
        # `fecha_expedicion` no es decorado: `ck_flota_doc_estado_coherente`
        # exige que un documento `vigente` diga cuándo se expidió, tenga número
        # y tenga entidad. Un papel sin eso no es un papel.
        d = DocumentoVehiculo(vehiculo_id=mundo['veh'], tipo=tipo,
                              numero='X-1', entidad='CDA',
                              fecha_expedicion=vencimiento - timedelta(days=365),
                              fecha_vencimiento=vencimiento, estado='vigente')
        db.session.add(d)
        db.session.commit()
        return d

    def test_el_caso_real_la_RTM_vencida_desde_noviembre(self, app, db, mundo):
        """Antes del 2026-09-01 este barrido devolvía `en_ventana: 0` y no
        mandaba nada. El camión llevaba diez meses ilegal en silencio."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        self._doc(db, mundo, date(2025, 11, 11))
        r = barrer_documentos_por_vencer(hoy=_HOY)

        assert r['en_ventana'] == 0, 'un vencido no es un "por vencer"'
        assert r['vencidos_en_ventana'] == 1, (
            'el documento vencido no entró al barrido: sigue el silencio')
        assert r['enviados'] == 1

    def test_los_dos_avisos_no_se_deduplican_entre_si(self, app, db, mundo):
        """Un documento que pasó de por-vencer a vencido avisa las dos veces.

        Lo que lo garantiza es el **hito**, no el prefijo de plantilla: una
        fecha ISO y una semana ISO no pueden coincidir. El prefijo se verifica
        aparte, en `test_la_clave_dice_de_cual_aviso_se_trata`."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.modelos import Aviso

        doc = self._doc(db, mundo, _HOY + timedelta(days=5))
        barrer_documentos_por_vencer(hoy=_HOY)          # avisa por-vencer
        assert Aviso.query.count() == 1

        # el mismo documento, ya vencido
        doc.fecha_vencimiento = _HOY - timedelta(days=1)
        db.session.commit()
        r = barrer_documentos_por_vencer(hoy=_HOY)
        assert r['vencidos_en_ventana'] == 1
        assert r['enviados'] == 1, (
            'el aviso de vencido se dedupló contra el de por-vencer')
        assert Aviso.query.count() == 2

    def test_la_clave_dice_de_cual_aviso_se_trata(self, app, db, mundo):
        """El prefijo no protege la deduplicación — la protege el hito. Sirve
        para que `flota_aviso` distinga las dos poblaciones: «avisé que vence»
        y «avisé que está vencido» son hechos distintos, y contarlos juntos
        esconde el segundo, que es el grave.

        Es el mismo criterio por el que `documentos_no_encontrados` está
        separado de `documentos_vencidos` en el health."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.modelos import Aviso

        self._doc(db, mundo, date(2025, 11, 11))
        barrer_documentos_por_vencer(hoy=_HOY)

        claves = [a.clave for a in Aviso.query.all()]
        assert claves, 'no se registró ningún aviso'
        assert all(c.startswith('flota_documento_vencido:') for c in claves), (
            f'la clave no identifica al aviso de vencido: {claves}. El registro '
            f'no puede separar «vence pronto» de «ya está vencido».')

    def test_insiste_a_la_semana_siguiente(self, app, db, mundo):
        """Lo que hace que un mensaje perdido no sea silencio para siempre."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        self._doc(db, mundo, date(2025, 11, 11))
        r1 = barrer_documentos_por_vencer(hoy=_HOY)
        assert r1['enviados'] == 1

        r2 = barrer_documentos_por_vencer(hoy=_HOY + timedelta(days=7))
        assert r2['enviados'] == 1, (
            'no insistió a la semana siguiente: si el primer aviso se pierde, '
            'el camión queda ilegal y nadie vuelve a decir nada')

    def test_no_repite_dentro_de_la_misma_semana(self, app, db, mundo):
        """El cron corre todos los días. Sin esto, el chat se silencia en tres
        días y el aviso que importa llega a un silencio."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        self._doc(db, mundo, date(2025, 11, 11))
        barrer_documentos_por_vencer(hoy=_HOY)
        r = barrer_documentos_por_vencer(hoy=_HOY + timedelta(days=1))
        assert r['enviados'] == 0
        assert r['ya_avisados'] == 1

    def test_renovarlo_apaga_el_aviso(self, app, db, mundo):
        """La alarma se apaga sola cuando se resuelve — no hay que acordarse de
        silenciarla, que es como se apagan las alarmas para siempre."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer

        doc = self._doc(db, mundo, date(2025, 11, 11))
        barrer_documentos_por_vencer(hoy=_HOY)

        doc.fecha_vencimiento = _HOY + timedelta(days=365)
        db.session.commit()
        r = barrer_documentos_por_vencer(hoy=_HOY + timedelta(days=7))
        assert r['vencidos_en_ventana'] == 0
        assert r['enviados'] == 0


class TestNoRompeLoQueYaFuncionaba:
    """Detector en las dos direcciones."""

    @pytest.fixture
    def mundo(self, db, monkeypatch):
        from app.models.vehiculo import Vehiculo
        monkeypatch.setenv('FLOTA_AVISOS', 'true')
        monkeypatch.setenv('FLOTA_AVISO_TELEFONOS',
                           '{"mantenimiento": ["573001112233"]}')
        v = Vehiculo(placa='VENC-02', tipo='NHR', activo=True)
        db.session.add(v)
        db.session.commit()
        return {'veh': v.id}

    def test_el_aviso_de_por_vencer_sigue_igual(self, app, db, mundo):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.modelos import DocumentoVehiculo

        db.session.add(DocumentoVehiculo(
            vehiculo_id=mundo['veh'], tipo='soat', numero='S-1', entidad='X',
            fecha_expedicion=_HOY - timedelta(days=360),
            fecha_vencimiento=_HOY + timedelta(days=5), estado='vigente'))
        db.session.commit()

        r = barrer_documentos_por_vencer(hoy=_HOY)
        assert r['en_ventana'] == 1
        assert r['vencidos_en_ventana'] == 0
        assert r['enviados'] == 1

    def test_un_documento_lejano_no_avisa_por_ninguna_via(self, app, db, mundo):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.modelos import DocumentoVehiculo

        db.session.add(DocumentoVehiculo(
            vehiculo_id=mundo['veh'], tipo='soat', numero='S-2', entidad='X',
            fecha_expedicion=_HOY - timedelta(days=160),
            fecha_vencimiento=_HOY + timedelta(days=200), estado='vigente'))
        db.session.commit()

        r = barrer_documentos_por_vencer(hoy=_HOY)
        assert r['en_ventana'] == 0
        assert r['vencidos_en_ventana'] == 0
        assert r['enviados'] == 0

    def test_apagado_no_manda_nada(self, app, db, mundo, monkeypatch):
        """Regla 10: nace apagado, y sigue apagado para la vía nueva."""
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.modelos import DocumentoVehiculo

        db.session.add(DocumentoVehiculo(
            vehiculo_id=mundo['veh'], tipo='rtm', numero='R-1', entidad='X',
            fecha_expedicion=date(2024, 11, 11),
            fecha_vencimiento=date(2025, 11, 11), estado='vigente'))
        db.session.commit()

        monkeypatch.delenv('FLOTA_AVISOS', raising=False)
        r = barrer_documentos_por_vencer(hoy=_HOY)
        assert r['enviados'] == 0
        assert 'motivo' in r
