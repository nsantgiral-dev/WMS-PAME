"""El dictamen mueve la mercancía. Los dos veredictos, a dos sitios distintos.

Antes de esto el veredicto se registraba y ahí quedaba: la mercancía llegaba,
se contaba, alguien decidía, y nadie la ubicaba nunca. El registro estaba; la
consecuencia no.

Las dos ramas son distintas en todo — bin de destino, documento a Siesa,
movimiento — así que las dos se prueban enteras. Y se prueba también que la
mercancía NO se duplique ni se pierda, que es lo que un reparto mal hecho
produce sin dar ningún error.
"""
from datetime import datetime

import pytest

from app.models.almacen import Almacen
from app.models.inventario import MovimientoInventario, UbicacionProducto
from app.models.siesa_job import SiesaJob
from app.models.traslado import (ClaseTraslado, EstadoTraslado,
                                 ItemSolicitudTraslado, SolicitudTraslado)
from app.models.ubicacion import Ubicacion
from app.models.usuario import Usuario
from app.services.traslado_service import BODEGA_AVERIAS_DESTINO, TrasladoService


def _usuario(db, nombre, rol, bodega=None):
    u = Usuario(nombre=nombre, email=f'{nombre.replace(" ", ".").lower()}@t.co',
                rol=rol, bodega_siesa_id=bodega)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def cd(db):
    """El CD con sus dos zonas: una vendible y una de averías."""
    a = Almacen.query.filter_by(bodega_siesa_id=BODEGA_AVERIAS_DESTINO).first()
    if not a:
        a = Almacen(codigo='CD-T', nombre='CD', bodega_siesa_id=BODEGA_AVERIAS_DESTINO)
        db.session.add(a)
        db.session.flush()
    from app.services.picking_service import campos_ubicacion_averias
    if not Ubicacion.query.filter_by(almacen_id=a.id, codigo='SIESA-GENERAL').first():
        db.session.add(Ubicacion(almacen_id=a.id, codigo='SIESA-GENERAL',
                                 tipo='estanteria', tipo_zona='GENERAL'))
    if not Ubicacion.query.filter_by(almacen_id=a.id, codigo='AVE-A1-EST01').first():
        db.session.add(Ubicacion(almacen_id=a.id, codigo='AVE-A1-EST01',
                                 **campos_ubicacion_averias()))
    db.session.commit()
    return a


@pytest.fixture
def tienda(db):
    return _usuario(db, 'Tienda NC1', 'tienda', 'NC1')


@pytest.fixture
def supervisor(db, cd):
    return _usuario(db, 'Supervisor CD', 'supervisor', BODEGA_AVERIAS_DESTINO)


@pytest.fixture
def prod(db, producto):
    producto.unidad_negocio_id = '001'
    producto.codigo_siesa = 'SKU-AVE-1'
    db.session.commit()
    return producto


@pytest.fixture
def entregada(db, cd, tienda, prod):
    """Avería declarada, aprobada, despachada y recibida: 7 salieron, 5 llegaron."""
    s = SolicitudTraslado(
        codigo='ST-AVE-1', bodega_origen_siesa='NC1',
        bodega_destino_siesa=BODEGA_AVERIAS_DESTINO,
        estado=EstadoTraslado.ENTREGADA, solicitante_id=tienda.id,
        clase_traslado=ClaseTraslado.AVERIAS,
        averia_evidencia='fotos del punto', fecha_entrega=datetime.utcnow())
    db.session.add(s)
    db.session.flush()
    db.session.add(ItemSolicitudTraslado(
        solicitud_id=s.id, producto_id=prod.id,
        producto_codigo_siesa=prod.codigo_siesa,
        cantidad_solicitada=7, cantidad_aprobada=7,
        cantidad_enviada=7, cantidad_recibida=5,
        motivo_averia='empaque reventado'))
    db.session.commit()
    return s


def _bin(cd, codigo):
    return Ubicacion.query.filter_by(almacen_id=cd.id, codigo=codigo).one()


def _stock(ub, prod):
    r = UbicacionProducto.query.filter_by(ubicacion_id=ub.id,
                                          producto_id=prod.id).all()
    return sum(x.cantidad for x in r)


class TestVeredictoConfirmado:
    """Sí estaba averiada → zona de averías + documento a AV1."""

    def test_la_mercancia_aterriza_en_la_zona_de_averias(self, db, cd, entregada,
                                                         supervisor, prod):
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 5
        assert _stock(_bin(cd, 'SIESA-GENERAL'), prod) == 0

    def test_entra_lo_RECIBIDO_no_lo_enviado(self, db, cd, entregada,
                                             supervisor, prod):
        """Salieron 7, llegaron 5. Ubicar 7 sería inventar dos unidades que
        nadie contó — y el CHECK de la cadena existe precisamente porque ese
        error no da ningún error."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 5

    def test_sale_el_documento_a_av1(self, db, cd, entregada, supervisor):
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        jobs = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').all()
        assert len(jobs) == 1, 'el segundo tramo NB1→AV1 no se encoló'

    def test_el_movimiento_dice_de_donde_vino_y_por_que(self, db, cd, entregada,
                                                        supervisor, prod):
        """Quien lea el kardex dentro de seis meses no tiene el contexto de hoy."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True,
                                          nota='revisado en muelle')
        mov = MovimientoInventario.query.filter_by(
            numero_documento='ST-AVE-1').one()
        assert 'ST-AVE-1' in mov.motivo
        assert 'NC1' in mov.motivo
        assert 'empaque reventado' in mov.motivo
        assert mov.usuario_id == supervisor.id
        assert mov.saldo_antes == 0 and mov.saldo_despues == 5


class TestVeredictoRechazado:
    """No estaba averiada → vuelve a vendible, y NO sale ningún documento."""

    def test_vuelve_al_inventario_vendible(self, db, cd, entregada,
                                           supervisor, prod):
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, False,
                                          nota='estaba bien, error del punto')
        assert _stock(_bin(cd, 'SIESA-GENERAL'), prod) == 5
        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 0

    def test_NO_sale_documento_a_siesa(self, db, cd, entregada, supervisor):
        """El ETS de la recepción ya metió esas unidades en la existencia
        normal de la bodega. Mandar un TRA a AV1 las sacaría de donde el ERP
        correctamente ya las tiene."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, False,
                                          nota='estaba bien')
        assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0

    def test_el_movimiento_deja_la_nota_de_quien_contradijo(self, db, cd,
                                                            entregada, supervisor):
        """Contradice a quien la declaró; esa persona tiene derecho a leerlo."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, False,
                                          nota='llegó sellada y sin golpes')
        mov = MovimientoInventario.query.filter_by(
            numero_documento='ST-AVE-1').one()
        assert 'llegó sellada' in mov.motivo
        assert 'NO averiada' in mov.motivo


class TestNiSePierdeNiSeDuplica:

    @pytest.mark.parametrize('confirmada', [True, False])
    def test_la_mercancia_entra_UNA_vez(self, db, cd, entregada, supervisor,
                                        prod, confirmada):
        """Un reparto mal hecho duplica sin dar error: el total de la red sube
        y ningún guard lo nota."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, confirmada)
        total = (_stock(_bin(cd, 'AVE-A1-EST01'), prod)
                 + _stock(_bin(cd, 'SIESA-GENERAL'), prod))
        assert total == 5
        assert MovimientoInventario.query.filter_by(
            numero_documento='ST-AVE-1').count() == 1

    def test_el_veredicto_congelado_impide_la_segunda_entrada(self, db, cd,
                                                              entregada,
                                                              supervisor, prod):
        """El congelado del veredicto es lo que hace que esto no necesite su
        propia idempotencia: no hay segunda ejecución posible."""
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        otro = _usuario(db, 'Jefe CD', 'jefe_almacen', BODEGA_AVERIAS_DESTINO)
        with pytest.raises(ValueError, match='ya fue dictaminado'):
            TrasladoService.dictaminar_averia(entregada.id, otro.id, True)
        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 5

    def test_se_escribe_SIN_lote(self, db, cd, entregada, supervisor, prod):
        """Los tres filtros del sync miran `lote IS NULL`
        (`inventario_siesa_service.py:853,889,1058`). Una fila CON lote es
        invisible para los tres: no se actualiza nunca y el bulk zero no la
        toca. Quedaría contada por el WMS para siempre — doble conteo
        permanente y sin causa a la vista."""
        entregada.items[0].lote = 'L-2026-09'
        db.session.commit()
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        filas = UbicacionProducto.query.filter_by(producto_id=prod.id).all()
        assert filas, 'no se escribió nada'
        assert all(f.lote is None for f in filas), (
            f'fila con lote: {[f.lote for f in filas]} — invisible para el sync')


class TestCuandoNoSePuedeUbicar:
    """Regla 0: fallar conservador Y declararlo."""

    def test_sin_almacen_en_destino_el_dictamen_no_revienta(self, db, tienda,
                                                            prod, caplog):
        """Un 500 acá dejaría el veredicto sin guardar. Se registra, se declara
        en el log, y la mercancía queda para resolver a mano — que es lo que
        de verdad pasa cuando una bodega no está dada de alta en el WMS."""
        s = SolicitudTraslado(
            codigo='ST-AVE-HUERFANA', bodega_origen_siesa='NC1',
            bodega_destino_siesa='ZZZ', estado=EstadoTraslado.ENTREGADA,
            solicitante_id=tienda.id, clase_traslado=ClaseTraslado.AVERIAS,
            averia_evidencia='x')
        db.session.add(s)
        db.session.flush()
        db.session.add(ItemSolicitudTraslado(
            solicitud_id=s.id, producto_id=prod.id,
            producto_codigo_siesa=prod.codigo_siesa,
            cantidad_solicitada=2, cantidad_aprobada=2,
            cantidad_enviada=2, cantidad_recibida=2, motivo_averia='roto'))
        db.session.commit()
        admin = _usuario(db, 'Administrador', 'admin', None)

        s = TrasladoService.dictaminar_averia(s.id, admin.id, True)
        assert s.averia_veredicto is True
        assert 'NO se ubicó' in caplog.text
        assert MovimientoInventario.query.filter_by(
            numero_documento='ST-AVE-HUERFANA').count() == 0

    def test_una_linea_sin_cantidad_recibida_no_escribe_nada(self, db, cd,
                                                             entregada,
                                                             supervisor, prod):
        """Cero unidades no es un movimiento de cero: es no haber movido nada.
        Un `MovimientoInventario` con cantidad 0 es ruido en el kardex."""
        entregada.items[0].cantidad_recibida = 0
        entregada.items[0].cantidad_enviada = 0
        db.session.commit()
        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        assert MovimientoInventario.query.filter_by(
            numero_documento='ST-AVE-1').count() == 0


class TestNoQuedaContadoDosVeces:
    """El defecto que encontró la auditoría del propio trabajo.

    Confirmar una avería SUMABA al bin de averías sin RESTAR del bucket
    vendible donde el sync ya había puesto esas unidades. Las mismas N unidades
    quedaban contadas dos veces en la misma bodega.

    Y no era un parpadeo: el sync de inventario corre **una vez al día, a las
    7am** (`inventario_siesa_service._programar_carga_diaria`). El doble conteo
    duraba hasta 21 horas, y en todo ese rato NB1 prometía stock vendible que
    estaba físicamente en el rincón de averías.
    """

    def test_confirmar_descuenta_del_bucket_vendible(self, db, cd, entregada,
                                                     supervisor, prod):
        """La carga de las 7am ya había traído las 5 unidades a SIESA-GENERAL.
        Al confirmarlas como averiadas tienen que SALIR de ahí."""
        gen = _bin(cd, 'SIESA-GENERAL')
        db.session.add(UbicacionProducto(ubicacion_id=gen.id,
                                         producto_id=prod.id, cantidad=5))
        db.session.commit()

        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)

        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 5
        assert _stock(gen, prod) == 0, 'quedaron contadas dos veces'

    def test_el_total_de_la_bodega_no_cambia_al_confirmar(self, db, cd,
                                                          entregada,
                                                          supervisor, prod):
        """Un dictamen mueve mercancía de zona; no crea ni destruye unidades.
        Un cuadre por sumas no detecta el error del reparto, pero sí detecta
        que el total se mueva — y acá no debe moverse."""
        gen = _bin(cd, 'SIESA-GENERAL')
        db.session.add(UbicacionProducto(ubicacion_id=gen.id,
                                         producto_id=prod.id, cantidad=12))
        db.session.commit()

        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)

        total = _stock(gen, prod) + _stock(_bin(cd, 'AVE-A1-EST01'), prod)
        assert total == 12, f'la bodega pasó de 12 a {total} unidades'

    def test_descuenta_del_bin_mas_grande_no_de_cinco_chicos(self, db, cd,
                                                             entregada,
                                                             supervisor, prod):
        """Corregir un saldo no es elegir qué despachar: repartir el ajuste
        entre huecos chicos ensucia cinco filas del kardex por una avería.

        **Hacen falta DOS bins para que este test signifique algo.** La primera
        versión ponía las 50 unidades en un solo bin, así que el `order_by` no
        se ejercitaba nunca: una mutación que lo invertía a ascendente dejaba
        el test en verde. Acá el bin chico tiene justo menos que la cantidad a
        descontar, así que un orden ascendente lo vaciaría y partiría el ajuste
        en dos filas — que es exactamente lo que se quiere evitar.
        """
        gen = _bin(cd, 'SIESA-GENERAL')
        chico = Ubicacion(almacen_id=cd.id, codigo='A-01-01',
                          tipo='estanteria', tipo_zona='GENERAL')
        db.session.add(chico)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=gen.id,
                                         producto_id=prod.id, cantidad=50))
        db.session.add(UbicacionProducto(ubicacion_id=chico.id,
                                         producto_id=prod.id, cantidad=3))
        db.session.commit()

        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)

        assert _stock(gen, prod) == 45, 'no salió del bin más grande'
        assert _stock(chico, prod) == 3, (
            'el ajuste se repartió: el bin chico se tocó sin necesidad')

    def test_si_el_bucket_no_alcanza_no_queda_negativo_y_se_declara(
            self, db, cd, entregada, supervisor, prod, caplog):
        """Regla 0 en el borde real: dictaminar el MISMO día, antes de que la
        carga de las 7am haya traído esas unidades desde Siesa.

        Quedar corto promete de menos; no descontar promete de más. De los dos
        errores, solo uno manda a alguien a buscar mercancía que no está.
        """
        gen = _bin(cd, 'SIESA-GENERAL')
        db.session.add(UbicacionProducto(ubicacion_id=gen.id,
                                         producto_id=prod.id, cantidad=2))
        db.session.commit()

        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)

        assert _stock(gen, prod) == 0, 'no puede quedar negativo'
        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 5
        assert 'Faltan 3' in caplog.text
        assert '7am' in caplog.text

    def test_NO_averiada_no_descuenta_nada(self, db, cd, entregada,
                                           supervisor, prod):
        """La mercancía que resultó sana se queda donde está y donde Siesa la
        tiene. Descontarla la haría desaparecer del pool sin razón."""
        gen = _bin(cd, 'SIESA-GENERAL')
        db.session.add(UbicacionProducto(ubicacion_id=gen.id,
                                         producto_id=prod.id, cantidad=5))
        db.session.commit()

        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, False,
                                          nota='estaba bien')
        # 5 que ya estaban + 5 que se ubican al dictaminar.
        assert _stock(gen, prod) == 10
        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 0

    def test_no_toca_el_stock_de_OTRO_almacen(self, db, cd, entregada,
                                              supervisor, prod):
        """El descuento se acota al almacén que firma el movimiento. Sin ese
        recorte, confirmar una avería en el CD vaciaría el bin de un punto —el
        total de la red no cambiaría y ningún cuadre por sumas lo vería."""
        otro = Almacen(codigo='PV-X', nombre='Punto X', bodega_siesa_id='PCX')
        db.session.add(otro)
        db.session.flush()
        ub_otro = Ubicacion(almacen_id=otro.id, codigo='PCX-GENERAL',
                            tipo='estanteria', tipo_zona='GENERAL')
        db.session.add(ub_otro)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ub_otro.id,
                                         producto_id=prod.id, cantidad=99))
        db.session.commit()

        TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        assert _stock(ub_otro, prod) == 99


class TestSiElDocumentoNoSaleSeDice:
    """Un warning en el log no es un canal: nadie lo mira hasta que ya está
    buscando por qué no cuadra.

    `encolar_traslado_averias` devuelve False —sin levantar nada— cuando falta
    `codigo_siesa` o cuando la bodega no es la que el conector 142951 sabe
    emitir. Descartando ese valor, la tarjeta decía «✓ Confirmada como
    averiada» exactamente igual se hubiera emitido el documento o no, mientras
    Siesa sigue contando esas unidades como vendibles en el CD.
    """

    def test_sin_codigo_siesa_el_veredicto_lo_declara(self, db, cd, entregada,
                                                      supervisor, prod):
        """Sin código Siesa no hay documento posible. La mercancía igual se
        ubica —perderla sería peor— pero el traslado tiene que decirlo."""
        entregada.items[0].producto_codigo_siesa = None
        prod.codigo_siesa = None
        db.session.commit()

        s = TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)

        assert s.averia_veredicto is True
        assert _stock(_bin(cd, 'AVE-A1-EST01'), prod) == 5, 'no se ubicó'
        assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0
        assert s.siesa_error, 'el fallo quedó solo en el log'
        assert 'SIN documento' in s.siesa_error
        assert 'Resolver a mano' in s.siesa_error

    def test_el_aviso_dice_donde_quedaron_contadas(self, db, cd, entregada,
                                                   supervisor, prod):
        """Quien lea esto tiene que saber qué arreglar: la mercancía está en la
        zona de averías del WMS y Siesa la sigue contando en el CD."""
        entregada.items[0].producto_codigo_siesa = None
        prod.codigo_siesa = None
        db.session.commit()

        s = TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        assert BODEGA_AVERIAS_DESTINO in s.siesa_error

    def test_cuando_el_documento_SI_sale_no_se_inventa_un_error(self, db, cd,
                                                                entregada,
                                                                supervisor):
        """La otra dirección. Un aviso que aparece en la operación sana se
        vuelve ruido, y el canal deja de leerse — la lección de los 639."""
        s = TrasladoService.dictaminar_averia(entregada.id, supervisor.id, True)
        assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 1
        assert not s.siesa_error

    def test_un_veredicto_NO_averiada_tampoco_inventa_error(self, db, cd,
                                                            entregada,
                                                            supervisor):
        """No sale documento porque no corresponde, no porque haya fallado."""
        s = TrasladoService.dictaminar_averia(entregada.id, supervisor.id, False,
                                              nota='estaba bien')
        assert not s.siesa_error
