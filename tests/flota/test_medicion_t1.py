"""
Cada campo del health se prueba MOVIENDO UN DATO REAL.

Un campo que devuelve una constante plausible es indistinguible de uno medido
hasta el día en que importa — y ese día es siempre el peor. La única forma de
distinguirlos es cambiar el mundo y exigir que el número cambie con él.

El patrón nació en el paso 1 para `conductores_activos_sin_cuenta` y acá se
aplica a los siete campos que dejaron de ser `null` al crearse las tablas. Cada
test hace lo mismo: lee, escribe un dato de verdad, vuelve a leer, y compara
contra el delta esperado. Nunca contra un valor absoluto — un absoluto acopla el
test al estado de la base y se rompe por razones que no son el bug.
"""
from datetime import date, datetime, timedelta

from app.utils.fecha import dia_operativo

import pytest

from flota.adaptadores.medicion import FOTOS_POR_CUSTODIA, MedidorSQL

_T0 = datetime(2026, 8, 1, 5, 0)


@pytest.fixture
def mundo(db):
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='TGZ655', tipo='NHR', activo=True)
    alm = Almacen(codigo='MED-FLOTA', nombre='Sede medición')
    usr = Usuario(email='flota_med@test.com', nombre='Gestor', rol='admin', activo=True)
    usr.set_password('x')
    db.session.add_all([veh, alm, usr])
    db.session.flush()
    con = Conductor(nombre='Conductor MED', cedula='MED-9001', activo=True,
                    usuario_id=usr.id)
    db.session.add(con)
    db.session.commit()
    return {'db': db, 'vehiculo': veh, 'almacen': alm, 'usuario': usr, 'conductor': con}


def _ficha(mundo, **kw):
    from flota.adaptadores.modelos import FichaTecnica

    campos = dict(
        vehiculo_id=mundo['vehiculo'].id, combustible='diesel',
        sistema_frenos='hidraulico', frenos_fuente='manual_fabricante',
        tiene_freno_escape='no', distribucion='correa',
        transmision_final='cardan',
        distribucion_fuente='concesionario', aceite_motor_spec='15W40 CI-4',
        posiciones_llanta=4, tiene_furgon=False,
        km_inicial=100_000, km_inicial_ts=_T0,
    )
    campos.update(kw)
    f = FichaTecnica(**campos)
    mundo['db'].session.add(f)
    mundo['db'].session.commit()
    return f


def _custodia(mundo, inicio=0, fin=None, pendiente_sede=False):
    from flota.adaptadores.modelos import Custodia

    if pendiente_sede:
        c = Custodia(
            vehiculo_id=mundo['vehiculo'].id, custodio_tipo='sede',
            custodio_estado='pendiente_sede',
            registrado_por_usuario_id=mundo['usuario'].id,
            inicio_ts=_T0 + timedelta(minutes=inicio),
            fin_ts=None if fin is None else _T0 + timedelta(minutes=fin),
            km_inicio=100_000,
        )
        mundo['db'].session.add(c)
        mundo['db'].session.commit()
        return c

    c = Custodia(
        vehiculo_id=mundo['vehiculo'].id, custodio_tipo='conductor',
        custodio_conductor_id=mundo['conductor'].id,
        registrado_por_usuario_id=mundo['usuario'].id,
        inicio_ts=_T0 + timedelta(minutes=inicio),
        fin_ts=None if fin is None else _T0 + timedelta(minutes=fin),
        km_inicio=100_000,
    )
    mundo['db'].session.add(c)
    mundo['db'].session.commit()
    return c


def _fotos(mundo, entidad_tipo, entidad_id, cuantas, estado='ok'):
    from flota.adaptadores.modelos import Foto

    for i in range(cuantas):
        mundo['db'].session.add(Foto(
            clase='evidencia_estado', entidad_tipo=entidad_tipo,
            entidad_id=entidad_id, storage_ref=f's3://flota/{entidad_id}-{i}.jpg',
            hash_sha256='0' * 64, bytes=1000, ancho=800, alto=600,
            mime='image/jpeg', ts_captura=_T0,
            autor_usuario_id=mundo['usuario'].id, estado=estado,
        ))
    mundo['db'].session.commit()


def _documento(mundo, vence_en_dias):
    from flota.adaptadores.modelos import DocumentoVehiculo

    d = DocumentoVehiculo(
        vehiculo_id=mundo['vehiculo'].id, tipo='soat', numero=f'N{vence_en_dias}',
        entidad='Aseguradora', fecha_expedicion=dia_operativo() - timedelta(days=365),
        fecha_vencimiento=dia_operativo() + timedelta(days=vence_en_dias),
    )
    mundo['db'].session.add(d)
    mundo['db'].session.commit()
    return d


# ══════════════════════════════════════════════════════════════════════════

class TestFichasCompletas:

    def test_una_ficha_con_huecos_no_cuenta_como_completa(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.fichas_completas()
        _ficha(mundo, distribucion='sin_dato', distribucion_fuente='sin_dato')
        assert medidor.fichas_completas() == antes, (
            'Una ficha con un atributo en sin_dato es una fila, no un dato.'
        )

    def test_una_ficha_sin_huecos_si_cuenta(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.fichas_completas()
        _ficha(mundo)
        assert medidor.fichas_completas() == antes + 1


class TestAtributosSinDato:

    def test_aparece_con_la_placa_y_no_con_el_id(self, mundo):
        """Quien lee esto va a buscar el camión, no la fila."""
        _ficha(mundo, distribucion='sin_dato', distribucion_fuente='sin_dato')
        assert 'TGZ655.distribucion' in MedidorSQL().atributos_sin_dato()

    def test_un_atributo_nuevo_deja_incompleta_una_ficha_que_lo_estaba(self, mundo):
        """`transmision_final` (2026-08-01) entró como `sin_dato` por defecto.

        Una ficha que estaba completa antes del campo nuevo NO sigue completa
        después: le falta un dato que ahora se pide. Un default que la dejara
        pasar convertiría el campo en decorativo el día que se agregó.
        """
        medidor = MedidorSQL()
        antes = medidor.fichas_completas()
        _ficha(mundo, transmision_final='sin_dato')
        assert medidor.fichas_completas() == antes

    def test_una_ficha_completa_no_aporta_nada(self, mundo):
        medidor = MedidorSQL()
        antes = len(medidor.atributos_sin_dato())
        _ficha(mundo)
        assert len(medidor.atributos_sin_dato()) == antes


class TestVehiculosSinCustodiaActiva:

    def test_el_vehiculo_nuevo_cuenta_y_deja_de_contar_al_abrirle_custodia(self, mundo):
        medidor = MedidorSQL()
        con_vehiculo_nuevo = medidor.vehiculos_sin_custodia_activa()
        _custodia(mundo, inicio=0)
        assert medidor.vehiculos_sin_custodia_activa() == con_vehiculo_nuevo - 1

    def test_una_custodia_cerrada_no_cubre(self, mundo):
        """Cerrada es histórico, no responsabilidad vigente."""
        medidor = MedidorSQL()
        antes = medidor.vehiculos_sin_custodia_activa()
        _custodia(mundo, inicio=0, fin=120)
        assert medidor.vehiculos_sin_custodia_activa() == antes


class TestCustodiasSinFotoCompleta:

    def test_una_custodia_sin_fotos_cuenta(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.custodias_sin_foto_completa()
        _custodia(mundo, inicio=0)
        assert medidor.custodias_sin_foto_completa() == antes + 1

    def _exigidas(self, mundo):
        """Cuántas fotos pide el SISTEMA para este vehículo — del dominio.

        La versión anterior importaba `FOTOS_POR_CUSTODIA` y afirmaba contra
        ella: verificaba la implementación, no la regla. Por eso estaba en verde
        mientras el health daba por completa una custodia con 9 de 13 — y lo que
        faltaba eran **posiciones de llanta**, que es donde está la tuerca floja
        que el registro existe para atribuir.
        """
        from flota.adaptadores.modelos import FichaTecnica
        from flota.dominio.valores import angulos_de_custodia, posiciones_llanta

        v = mundo['vehiculo']
        ficha = FichaTecnica.query.filter_by(vehiculo_id=v.id).first()
        n, _ = posiciones_llanta(ficha.posiciones_llanta if ficha else None, v.tipo)
        return len(angulos_de_custodia(n))

    def test_una_de_menos_todavia_cuenta(self, mundo):
        """El ángulo que falta es justo el que se discute después del golpe."""
        medidor = MedidorSQL()
        antes = medidor.custodias_sin_foto_completa()
        c = _custodia(mundo, inicio=0)
        _fotos(mundo, 'custodia_inicio', c.id, self._exigidas(mundo) - 1)
        assert medidor.custodias_sin_foto_completa() == antes + 1

    def test_con_todas_las_del_dominio_deja_de_contar(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.custodias_sin_foto_completa()
        c = _custodia(mundo, inicio=0)
        _fotos(mundo, 'custodia_inicio', c.id, self._exigidas(mundo))
        assert medidor.custodias_sin_foto_completa() == antes

    def test_con_OCHO_no_alcanza_si_el_vehiculo_pide_mas(self, mundo):
        """El defecto exacto, en un test.

        Ocho era el modelo viejo —una sola foto para todas las llantas—. Un
        camión pide 13 y un furgón 11: con ocho faltan posiciones de rueda, y el
        health las daba por cubiertas.
        """
        import pytest as _pytest
        exigidas = self._exigidas(mundo)
        if exigidas <= 8:
            _pytest.skip(f'este vehículo pide {exigidas}, no aplica')
        medidor = MedidorSQL()
        antes = medidor.custodias_sin_foto_completa()
        c = _custodia(mundo, inicio=0)
        _fotos(mundo, 'custodia_inicio', c.id, 8)
        assert medidor.custodias_sin_foto_completa() == antes + 1, (
            f'con 8 fotos dio por completa una custodia que pide {exigidas} — '
            f'faltan posiciones de llanta y nadie lo ve')


class TestFotosPendienteEvidencia:

    def test_una_foto_declarada_rota_se_cuenta(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.fotos_pendiente_evidencia()
        c = _custodia(mundo, inicio=0)
        _fotos(mundo, 'custodia_inicio', c.id, 1, estado='pendiente_evidencia')
        assert medidor.fotos_pendiente_evidencia() == antes + 1

    def test_una_foto_sana_no(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.fotos_pendiente_evidencia()
        c = _custodia(mundo, inicio=0)
        _fotos(mundo, 'custodia_inicio', c.id, 1)
        assert medidor.fotos_pendiente_evidencia() == antes


class TestDocumentos:

    def test_un_documento_vencido_se_cuenta(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.documentos_vencidos()
        _documento(mundo, vence_en_dias=-1)
        assert medidor.documentos_vencidos() == antes + 1

    def test_uno_que_vence_en_10_dias_va_al_otro_contador(self, mundo):
        medidor = MedidorSQL()
        vencidos, por_vencer = medidor.documentos_vencidos(), medidor.documentos_por_vencer_30d()
        _documento(mundo, vence_en_dias=10)
        assert medidor.documentos_vencidos() == vencidos
        assert medidor.documentos_por_vencer_30d() == por_vencer + 1

    def test_un_vencido_no_se_cuela_en_por_vencer(self, mundo):
        """Son dos números distintos a propósito.

        "Por vencer" es una tarea con plazo; "vencido" es un camión que no
        debería estar rodando. Sumarlos esconde el segundo dentro del primero.
        """
        medidor = MedidorSQL()
        antes = medidor.documentos_por_vencer_30d()
        _documento(mundo, vence_en_dias=-5)
        assert medidor.documentos_por_vencer_30d() == antes

    def test_uno_que_vence_en_60_dias_no_cuenta_todavia(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.documentos_por_vencer_30d()
        _documento(mundo, vence_en_dias=60)
        assert medidor.documentos_por_vencer_30d() == antes


class TestCustodiasPendienteSede:
    """El hueco de `almacenes` se cuenta, no se tolera."""

    def test_una_custodia_sin_sede_representable_se_cuenta(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.custodias_pendiente_sede()
        _custodia(mundo, inicio=0, pendiente_sede=True)
        assert medidor.custodias_pendiente_sede() == antes + 1

    def test_una_custodia_normal_no(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.custodias_pendiente_sede()
        _custodia(mundo, inicio=0)
        assert medidor.custodias_pendiente_sede() == antes

    def test_pendiente_sede_igual_cubre_al_vehiculo(self, mundo):
        """No saber QUÉ sede no es lo mismo que no tener custodio.

        El vehículo tiene responsable declarado —una sede— aunque el WMS no
        pueda nombrarla todavía. Contarlo como "sin custodia activa" diría que
        nadie responde, que es falso y más grave.
        """
        medidor = MedidorSQL()
        antes = medidor.vehiculos_sin_custodia_activa()
        _custodia(mundo, inicio=0, pendiente_sede=True)
        assert medidor.vehiculos_sin_custodia_activa() == antes - 1


# ══════════════════════════════════════════════════════════════════════════
# Las enumeraciones — «cuántos» y «cuáles» no pueden dejar de coincidir
# ══════════════════════════════════════════════════════════════════════════

class TestLosContadoresDerivanDeLaLista:
    """Cinco contadores que antes tenían su propio `filter` ahora se calculan de
    dos enumeraciones. La propiedad que hay que sostener es que **no se pueden
    separar**: si el número y la lista dejan de decir lo mismo, no hay forma de
    saber cuál creer, y el que decide mira el número mientras el que trabaja
    mira la lista.

    No alcanza con probar que el contador da bien: eso pasaría con las dos
    implementaciones separadas y divergiendo. Se prueba la **identidad** entre
    el contador y la longitud del filtro sobre la lista, que es lo que el
    refactor compró.
    """

    def test_documentos_el_numero_ES_la_longitud_de_la_lista(self, mundo):
        _documento(mundo, vence_en_dias=-30)
        _documento(mundo, vence_en_dias=-2)
        _documento(mundo, vence_en_dias=10)
        _documento(mundo, vence_en_dias=200)      # ni vencido ni por vencer
        m = MedidorSQL()
        filas = m.documentos_por_vehiculo()
        assert m.documentos_vencidos() == sum(1 for f in filas if f['vencido'])
        assert m.documentos_vencidos() == 2
        assert (m.documentos_por_vencer_30d()
                == sum(1 for f in filas if f['por_vencer_30d']) == 1)
        # El de 200 días no está al día *ni* pide trabajo: no entra a la lista.
        assert len(filas) == 3

    def test_la_base_PROHIBE_que_un_papel_este_vencido_y_no_encontrado(self, mundo):
        """El supuesto en el que descansan las tres banderas, afirmado contra la
        base y no dejado en un comentario.

        Se intentó escribir el caso «no encontrado y además vencido» para probar
        que contaba en los dos contadores. **No se puede construir**: el CHECK
        `ck_flota_doc_estado_coherente` exige `fecha_vencimiento IS NULL` cuando
        el estado es `no_encontrado`. Un papel que nadie pudo mostrar no tiene
        fecha que juzgar — es coherente.

        Queda escrito como test porque la disyunción es de la BASE y no del
        medidor: si el CHECK se relaja, este test se pone rojo y quien lo relaje
        va a tener que decidir a mano qué hacen los dos contadores, en vez de
        descubrirlo por un número que se movió.
        """
        import sqlalchemy.exc

        d = _documento(mundo, vence_en_dias=-5)
        d.estado = 'no_encontrado'
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            mundo['db'].session.commit()
        mundo['db'].session.rollback()

    def test_un_no_encontrado_SIN_fecha_entra_solo_a_su_contador(self, mundo):
        """La forma que la base sí admite. Confirma que las banderas de la fila
        y los contadores siguen diciendo lo mismo en el caso real."""
        from flota.adaptadores.modelos import DocumentoVehiculo

        mundo['db'].session.add(DocumentoVehiculo(
            vehiculo_id=mundo['vehiculo'].id, tipo='poliza_rc',
            estado='no_encontrado', numero=None, entidad=None,
            fecha_expedicion=None, fecha_vencimiento=None))
        mundo['db'].session.commit()

        m = MedidorSQL()
        fila = m.documentos_por_vehiculo()[0]
        assert (fila['no_encontrado'], fila['vencido'], fila['por_vencer_30d']) \
            == (True, False, False)
        assert m.documentos_no_encontrados() == 1
        assert m.documentos_vencidos() == 0

    def test_la_fila_trae_la_placa_que_es_para_lo_que_existe(self, mundo):
        """Sin la placa el número no dice a quién llamar, y «persigue lo
        vencido» es la definición escrita del trabajo del rol."""
        _documento(mundo, vence_en_dias=-1)
        fila = MedidorSQL().documentos_por_vehiculo()[0]
        assert fila['placa'] == mundo['vehiculo'].placa
        assert fila['dias'] < 0, 'el signo separa «sacá la cita» de «bajá el camión»'
        assert fila['base'] and fila['etiqueta'], 'regla 13: cada fila, no la página'

    def test_lo_peor_va_primero(self, mundo):
        _documento(mundo, vence_en_dias=-1)
        _documento(mundo, vence_en_dias=-40)
        assert [f['dias'] for f in MedidorSQL().documentos_por_vehiculo()] == [-40, -1]

    def test_custodias_el_numero_ES_la_longitud_de_la_lista(self, mundo):
        _custodia(mundo, inicio=0, fin=60)
        # El id se lee ANTES de mutar: leerlo después dispara un autoflush con
        # `cierre_forzado` ya en `True` y el autor todavía en `NULL`, que es
        # justo el estado que el CHECK rechaza.
        uid = mundo['usuario'].id
        c = _custodia(mundo, inicio=120, fin=180)
        # Los tres campos juntos: `ck_flota_cierre_forzado_declarado` rechaza un
        # forzado anónimo, y con razón — quedaría el rastro de que pasó algo
        # raro y ninguna forma de saber quién ni por qué.
        c.cierre_forzado_por_usuario_id = uid
        c.cierre_forzado_motivo = 'el conductor no volvió'
        c.cierre_forzado = True
        mundo['db'].session.commit()

        m = MedidorSQL()
        filas = m.custodias_por_vehiculo()
        assert (m.custodias_cerradas_forzadas()
                == sum(1 for f in filas if f['cierre_forzado']) == 1)
        assert (m.custodias_sin_foto_completa()
                == sum(1 for f in filas if f['sin_foto_completa']))
        forzada = next(f for f in filas if f['cierre_forzado'])
        assert forzada['placa'] == mundo['vehiculo'].placa
        assert forzada['cierre_forzado_motivo'] == 'el conductor no volvió'

    def test_una_custodia_con_las_DOS_mitades_incompletas_cuenta_UNA_vez(self, mundo):
        """El `elif` del predicado original, conservado y ahora afirmado.

        `custodias_sin_foto_completa` mide **turnos**, no mitades. Sin este
        test, alguien que «arregle» el `elif` a un `if` duplica el número sin
        que nada se ponga rojo — y el contador pasa a medir otra cosa con el
        mismo nombre.
        """
        _custodia(mundo, inicio=0, fin=60)      # cero fotos en las dos mitades
        m = MedidorSQL()
        assert m.custodias_sin_foto_completa() == 1
        filas = [f for f in m.custodias_por_vehiculo() if f['sin_foto_completa']]
        assert len(filas) == 1
        assert filas[0]['mitad_incompleta'] == 'inicio', (
            'con las dos incompletas se reporta la primera, que es la que hay '
            'que arreglar antes')

    def test_una_custodia_sana_no_entra_a_la_lista(self, mundo):
        """La otra dirección. Una lista que devolviera todas las custodias
        pasaría los tests de arriba y convertiría el panel en un volcado."""
        m = MedidorSQL()
        assert m.custodias_por_vehiculo() == []
        assert m.custodias_cerradas_forzadas() == 0


class TestLaEnumeracionNoEscalaEnConsultas:
    """El N+1 no vuelve por descuido: se cuentan las consultas, no el tiempo.

    `custodias_por_vehiculo` hacía un COUNT por custodia y **corre 3 veces por
    petición** de health (la publica como campo y la consultan sus dos
    contadores derivados). Medido el 2026-09-09 sobre SQLite con 6 vehículos:
    61 ms con 26 custodias, 128 con 200, 465 con 1000. Seis vehículos con dos
    turnos diarios producen ~4.400 custodias al año — dos segundos por petición
    dentro de doce meses.

    Se arregló quitando el N+1 y **no cacheando**: un medidor que cachea deja de
    medir y sigue contestando 200. Tras el cambio: 53 ms con 26, 113 con 1000,
    244 con 4.400.

    El test cuenta sentencias y no milisegundos a propósito. Un umbral de tiempo
    en CI mide la máquina; el número de consultas mide lo que se rompió.
    """

    def _contando(self, fn):
        from sqlalchemy import event

        from app.extensions import db

        sentencias = []

        def espia(conn, cursor, sql, params, contexto, muchos):
            sentencias.append(sql)

        event.listen(db.engine, 'before_cursor_execute', espia)
        try:
            fn()
        finally:
            event.remove(db.engine, 'before_cursor_execute', espia)
        return sentencias

    def test_el_numero_de_consultas_NO_crece_con_las_custodias(self, mundo):
        """La afirmación es de forma, no de magnitud: con cinco veces más
        custodias, el mismo número de consultas."""
        for k in range(3):
            _custodia(mundo, inicio=k * 100, fin=k * 100 + 50)
        pocas = len(self._contando(lambda: MedidorSQL().custodias_por_vehiculo()))

        for k in range(3, 15):
            _custodia(mundo, inicio=k * 100, fin=k * 100 + 50)
        muchas = len(self._contando(lambda: MedidorSQL().custodias_por_vehiculo()))

        assert pocas == muchas, (
            f'con 3 custodias hizo {pocas} consultas y con 15 hizo {muchas}: '
            f'volvió el N+1')

    def test_y_sigue_contando_bien_despues_de_agrupar(self, mundo):
        """La otra dirección. Una consulta agrupada mal escrita da un número
        estable y equivocado, que es peor que uno lento."""
        for k in range(4):
            _custodia(mundo, inicio=k * 100, fin=k * 100 + 50)
        m = MedidorSQL()
        # Cero fotos en todas: las cuatro están incompletas por el lado de inicio.
        assert m.custodias_sin_foto_completa() == 4
        assert all(f['mitad_incompleta'] == 'inicio'
                   for f in m.custodias_por_vehiculo())
