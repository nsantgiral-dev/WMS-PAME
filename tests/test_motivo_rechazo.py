"""
El rechazo dejó de ser el estado más barato, y ahora dice si la mercancía volvió.

Hasta el 2026-08-13 confirmar una parada pedía:

    ENTREGADO   → forma de pago
    PARCIAL     → forma de pago + monto > 0 + observaciones
    RECHAZADO   → **solo observaciones**, texto libre

Nadie decidió que devolver mercancía costara menos que cobrarla: salió de cómo
se fueron agregando las validaciones. Pero el control «si no paga completo, no
se entrega» lo vuelve peligroso — el conductor que no quiere subir bultos al
camión marca el estado que menos pide, y eso convierte **un faltante de
inventario en una devolución falsa**, que solo aparece en un conteo físico.

## La pregunta que el texto libre no hacía

¿Volvió la mercancía? Es lo único que cambia qué documento hace falta en Siesa,
y ninguna prosa lo contesta de forma contable.

## Por qué el caso peligroso tiene su propia opción

`NO_PAGO_SE_QUEDO` existe a propósito. Prohibir sin dar alternativa no elimina
un comportamiento: lo esconde. Si el conductor no puede declarar «no pagó y se
quedó con la mercancía», marca «cliente cerrado» y el dato miente sobre
inventario — que es peor que mentir sobre cartera, porque cartera se reconcilia
contra Siesa y el inventario solo contra un conteo físico.

Darle nombre no lo autoriza. Lo hace contable, que es el primer paso para que
alguien decida si se permite.
"""
import pytest

from app.services import motivos_rechazo as mr


class TestElCatalogo:

    def test_el_caso_peligroso_es_representable(self):
        """Si no tiene nombre, el conductor elige otra cosa y el dato miente."""
        assert 'NO_PAGO_SE_QUEDO' in mr.CODIGOS

    def test_distingue_no_pago_de_no_pago_y_se_quedo(self):
        """Son hechos distintos: en uno vuelve el inventario, en el otro no."""
        assert mr.retorna_mercancia('NO_PAGO') is True
        assert mr.retorna_mercancia('NO_PAGO_SE_QUEDO') is False

    def test_los_demas_motivos_devuelven_mercancia(self):
        for c in mr.CODIGOS:
            if c != 'NO_PAGO_SE_QUEDO':
                assert mr.retorna_mercancia(c) is True, c

    def test_un_codigo_desconocido_asume_que_vuelve(self):
        """Regla 0: tratar mercancía como perdida sin evidencia genera un
        ajuste de inventario que nadie pidió."""
        assert mr.retorna_mercancia('LO_QUE_SEA') is True
        assert mr.retorna_mercancia(None) is True

    def test_valido_rechaza_lo_que_no_esta(self):
        assert mr.valido('CLIENTE_CERRADO') is True
        assert mr.valido('cliente_cerrado') is True      # normaliza
        assert mr.valido('') is False
        assert mr.valido(None) is False
        assert mr.valido('INVENTADO') is False

    def test_sin_retorno_no_esta_hardcodeado_en_un_solo_sitio(self):
        """La lista existe para que agregar otro motivo sin retorno no exija
        encontrar todos los `== 'NO_PAGO_SE_QUEDO'` del código."""
        assert mr.SIN_RETORNO == ('NO_PAGO_SE_QUEDO',)


class TestElCatalogoLoSirveElBackend:
    """Dos catálogos del mismo dominio divergen. Ya pasó con la condición de
    pago (dos sitios, los dos hacia contado) y con los tipos de vehículo (el
    dominio conocía «camioneta», el formulario no la ofrecía)."""

    from pathlib import Path
    _RAIZ = Path(__file__).resolve().parents[1]

    def test_el_endpoint_devuelve_el_catalogo(self, client, jwt_token):
        r = client.get('/api/rutas/motivos-rechazo',
                       headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 200
        motivos = r.get_json()['motivos']
        assert len(motivos) == len(mr.CODIGOS)
        assert all({'codigo', 'etiqueta', 'retorna'} <= set(m) for m in motivos)

    def test_el_JS_no_escribe_su_propia_lista(self):
        js = (self._RAIZ / 'app' / 'static' / 'pwa' / 'rutas.js').read_text(encoding='utf-8')
        assert '/api/rutas/motivos-rechazo' in js, 'el JS dejó de pedir el catálogo'
        # Un catálogo paralelo se delata por tener varios códigos literales.
        literales = sum(1 for c in mr.CODIGOS if f"'{c}'" in js)
        assert literales <= 1, (
            f'el JS volvió a escribir la lista de motivos ({literales} códigos '
            f'literales) — usar el catálogo del backend')


@pytest.fixture
def recaudo_ctx(db, almacen):
    """Ruta EN_TRANSITO con una tarea y sus bultos — mínimo para confirmar."""
    import uuid

    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    from app.models.usuario import Usuario

    u = Usuario.query.filter_by(email='cond_mot@test.com').first()
    if not u:
        u = Usuario(email='cond_mot@test.com', nombre='Conductor',
                    rol='conductor', activo=True)
        u.set_password('test123')
        db.session.add(u); db.session.flush()

    ruta = RutaDespacho(conductor_id=u.id, tipo_ruta='Urbana', estado='EN_TRANSITO')
    db.session.add(ruta); db.session.flush()

    tarea = TareaPacking(codigo=f'PK-MOT-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                         almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa=555, numero_pedido_siesa='PED-MOT')
    db.session.add(tarea); db.session.flush()

    db.session.add(Bulto(tarea_id=tarea.id, ruta_despacho_id=ruta.id,
                         codigo_barras=f'B-{uuid.uuid4().hex[:6]}',
                         tipo='CAJA', numero=1, total=1, estado='EN_RUTA'))
    db.session.commit()
    return ruta, tarea, u.id


class TestLaValidacionAlConfirmar:

    def _data(self, **kw):
        base = {'estado_entrega': 'RECHAZADO', 'observaciones': 'no estaba'}
        base.update(kw)
        return base

    def test_rechazo_sin_motivo_tipificado_no_pasa(self, app, db, recaudo_ctx):
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = recaudo_ctx
        with pytest.raises(ValueError, match='motivo del rechazo'):
            RutaService.confirmar_parada(ruta.id, tarea.id, uid, self._data())

    def test_rechazo_con_motivo_inventado_no_pasa(self, app, db, recaudo_ctx):
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = recaudo_ctx
        with pytest.raises(ValueError, match='motivo del rechazo'):
            RutaService.confirmar_parada(ruta.id, tarea.id, uid,
                                         self._data(motivo_rechazo='PORQUE_SI'))

    def test_rechazo_sigue_exigiendo_el_detalle(self, app, db, recaudo_ctx):
        """El motivo tipificado no reemplaza a las observaciones: dice QUÉ
        pasó, no los detalles que hacen falta para reclamar."""
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = recaudo_ctx
        with pytest.raises(ValueError, match='detalle'):
            RutaService.confirmar_parada(
                ruta.id, tarea.id, uid,
                {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'CLIENTE_CERRADO',
                 'observaciones': '  '})

    def test_rechazo_valido_guarda_el_motivo(self, app, db, recaudo_ctx):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = recaudo_ctx
        rid, _ = RutaService.confirmar_parada(
            ruta.id, tarea.id, uid, self._data(motivo_rechazo='NO_PAGO_SE_QUEDO'))
        assert db.session.get(RecaudoEntrega, rid).motivo_rechazo == 'NO_PAGO_SE_QUEDO'

    def test_una_entrega_no_necesita_motivo(self, app, db, recaudo_ctx):
        """El requisito es del rechazo. Pedírselo a la entrega bloquearía la
        operación por un campo que no aplica — Regla 12."""
        from app.services.ruta_service import RutaService
        ruta, tarea, uid = recaudo_ctx
        rid, _ = RutaService.confirmar_parada(
            ruta.id, tarea.id, uid,
            {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
             'monto_cobrado': 1000})
        assert rid


class TestElModeloRechazaUnMotivoInventado:
    """El CHECK va en el modelo Y en la migración. Solo en la migración,
    `create_all()` no lo tendría y ningún test lo ejercitaría."""

    def test_la_tabla_declara_el_check(self):
        from app.models.recaudo_entrega import RecaudoEntrega
        nombres = {c.name for c in RecaudoEntrega.__table__.constraints}
        assert 'ck_recaudo_motivo_rechazo' in nombres
