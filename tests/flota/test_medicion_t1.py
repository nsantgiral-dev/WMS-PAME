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
        entidad='Aseguradora', fecha_expedicion=date.today() - timedelta(days=365),
        fecha_vencimiento=date.today() + timedelta(days=vence_en_dias),
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

    def test_siete_fotos_todavia_cuenta(self, mundo):
        """Siete de ocho no es "casi": el ángulo que falta es el que se discute."""
        medidor = MedidorSQL()
        antes = medidor.custodias_sin_foto_completa()
        c = _custodia(mundo, inicio=0)
        _fotos(mundo, 'custodia_inicio', c.id, FOTOS_POR_CUSTODIA - 1)
        assert medidor.custodias_sin_foto_completa() == antes + 1

    def test_con_las_ocho_deja_de_contar(self, mundo):
        medidor = MedidorSQL()
        antes = medidor.custodias_sin_foto_completa()
        c = _custodia(mundo, inicio=0)
        _fotos(mundo, 'custodia_inicio', c.id, FOTOS_POR_CUSTODIA)
        assert medidor.custodias_sin_foto_completa() == antes


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
