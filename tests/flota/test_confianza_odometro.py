"""
El tercer estado del kilómetro — `confianza`, sus vías de escritura y su cola.

`flota/dominio/odometro.py` tenía `confianza_al_nacer` y `confianza_del_tramo`
escritas, probadas… y **sin un solo caller y sin columna donde guardar el
resultado**. Es el patrón que este módulo lleva la semana pagando: una política
que decide algo que nadie persiste.

Este archivo prueba las tres mitades que faltaban:

1. **Que la marca se escriba SIEMPRE**, por cualquier vía. No hay una llamada
   por escritor —serían cinco copias de la misma regla y la sexta sería la que
   se olvida— sino un `before_insert` en el modelo. Acá se ejerce cada vía real
   por separado igual, porque «el gancho existe» y «esta vía pasa por el gancho»
   son dos afirmaciones distintas.
2. **Que `verificada` no la pueda escribir ningún automatismo.** En las dos
   direcciones: que la vía humana sí la escriba, y que la vía de INSERT no
   pueda — ni por ORM ni por SQL crudo.
3. **Que el CPK use la marca**: dos extremos dudosos no publican un número.

## Los guards, en las dos direcciones

Seis guards de este repo estuvieron verdes sobre propiedades que la vía sana
satisfacía por construcción (auditoría del 2026-08-15). Cada bloque de acá
tiene su par: el trigger que dispara **y** el `UPDATE` legítimo que NO tiene que
disparar; la ficha que no puede bajar **y** la ficha incoherente que sí se puede
seguir editando.
"""
import ast
import pathlib
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from flota.dominio.valores import Confianza

_RECHAZO = (IntegrityError, OperationalError)
_AUTH = lambda t: {'Authorization': f'Bearer {t}'}    # noqa: E731
_RAIZ = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def mundo(db, almacen):
    """Un vehículo, un conductor con cuenta, y los tres roles que importan."""
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='CNF100', tipo='NHR', activo=True)
    admin = Usuario(nombre='Admin CNF', email='cnf_admin@test.com',
                    password_hash=generate_password_hash('x'), rol='admin',
                    almacen_id=almacen.id, activo=True)
    cond_u = Usuario(nombre='Conductor CNF', email='cnf_cond@test.com',
                     password_hash=generate_password_hash('x'), rol='conductor',
                     almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control CNF', email='cnf_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    tienda = Usuario(nombre='Tienda CNF', email='cnf_tienda@test.com',
                     password_hash=generate_password_hash('x'), rol='tienda',
                     almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, admin, cond_u, flota, tienda])
    db.session.flush()
    cond = Conductor(nombre='Conductor CNF', cedula='CNF-1', activo=True,
                     usuario_id=cond_u.id)
    db.session.add(cond)
    db.session.commit()
    return {'placa': veh.placa, 'veh': veh.id, 'con': cond.id,
            'usuario_id': admin.id, 'cond_usuario_id': cond_u.id,
            'flota_usuario_id': flota.id,
            't_admin': create_access_token(identity=str(admin.id)),
            't_cond': create_access_token(identity=str(cond_u.id)),
            't_flota': create_access_token(identity=str(flota.id)),
            't_tienda': create_access_token(identity=str(tienda.id))}


def _lectura(db, mundo, km, ts=None, origen='cierre_dia', motivo=None):
    """Una lectura por el ORM, o sea por el gancho que marca la confianza."""
    from flota.adaptadores.modelos import LecturaOdometro

    fila = LecturaOdometro(vehiculo_id=mundo['veh'], valor_km=km,
                           ts=ts or datetime(2026, 3, 1, 8, 0), origen=origen,
                           autor_usuario_id=mundo['usuario_id'],
                           motivo_correccion=motivo)
    db.session.add(fila)
    db.session.commit()
    return fila


# ══════════════════════════════════════════════════════════════════════════
# 1 · La marca se escribe SIEMPRE, y por cualquier vía
# ══════════════════════════════════════════════════════════════════════════

class TestNingunaVíaDeEscrituraDejaLaLecturaSinMarcar:
    """Las cinco vías reales, una por una.

    No alcanza con probar el gancho: lo que hay que afirmar es que **cada vía
    pasa por él**. Un adaptador que mañana inserte con `bulk_save_objects` o con
    Core no dispararía el evento, y este archivo es donde eso se vería.
    """

    def test_las_vias_de_escritura_del_repo_son_las_que_este_archivo_ejerce(self):
        """Enumeradas por AST, no de memoria.

        Si aparece un sexto escritor, este test lo nombra y obliga a
        ejercitarlo. La lista NO está escrita a mano: se recorre `flota/`
        buscando construcciones de `LecturaOdometro(...)`, que es la única forma
        de crear una fila por el ORM.
        """
        sitios = set()
        for ruta in sorted((_RAIZ / 'flota').rglob('*.py')):
            arbol = ast.parse(ruta.read_text(encoding='utf-8'), filename=str(ruta))
            for nodo in ast.walk(arbol):
                if (isinstance(nodo, ast.Call)
                        and isinstance(nodo.func, ast.Name)
                        and nodo.func.id == 'LecturaOdometro'):
                    sitios.add(ruta.relative_to(_RAIZ).as_posix())

        assert sitios == {
            'flota/adaptadores/traspaso.py',      # recibo/entrega de turno
            'flota/adaptadores/hallazgos.py',     # anclar_odometro: hallazgo,
                                                  # inspección y gasto entran acá
            'flota/api/custodia.py',              # la lectura suelta
        }, (
            f'\nLas vías de escritura de lecturas cambiaron: {sorted(sitios)}\n'
            'Cada una tiene que quedar ejercida en este archivo: una vía que no '
            'marque confianza es la que va a meter la lectura mala.')

    def test_el_traspaso_de_turno_CON_foto_del_tablero_nace_declarada(
            self, client, mundo, db, tmp_path, monkeypatch):
        """La única vía que hoy adjunta la foto — y por eso la única que puede
        nacer `declarada`."""
        import base64
        import io

        from PIL import Image

        from flota.adaptadores.modelos import LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        buf = io.BytesIO()
        Image.new('RGB', (1600, 1200), (17, 17, 17)).save(buf, 'JPEG', quality=85)
        data_url = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()

        r = client.post('/flota/custodia/traspaso',
                        json={'placa': mundo['placa'], 'km': 500,
                              'custodio_tipo': 'conductor',
                              'custodio_conductor_id': mundo['con'],
                              'fotos_inicio': [{'clase': 'foto_dato',
                                                'angulo': 'tablero',
                                                'data_url': data_url,
                                                'ancho': 1600, 'alto': 1200}]},
                        headers=_AUTH(mundo['t_admin']))
        assert r.status_code in (200, 201), r.get_json()

        lec = LecturaOdometro.query.one()
        assert lec.foto_id is not None
        assert lec.confianza == Confianza.DECLARADA.value
        assert lec.motivo_dudosa is None

    def test_el_traspaso_SIN_foto_nace_dudosa_y_dice_por_que(
            self, client, mundo, db, tmp_path, monkeypatch):
        """El caso de producción: 26 de 26 lecturas sin foto (2026-09-01)."""
        from flota.adaptadores.modelos import LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        r = client.post('/flota/custodia/traspaso',
                        json={'placa': mundo['placa'], 'km': 500,
                              'custodio_tipo': 'conductor',
                              'custodio_conductor_id': mundo['con']},
                        headers=_AUTH(mundo['t_admin']))
        assert r.status_code in (200, 201), r.get_json()

        lec = LecturaOdometro.query.one()
        assert lec.confianza == Confianza.DUDOSA.value
        assert 'sin foto' in lec.motivo_dudosa

    def test_la_lectura_suelta_nace_marcada_y_la_respuesta_lo_dice(
            self, client, mundo):
        """La marca viaja en el 201: el momento de sacar la foto es ese,
        parado al lado del camión — no cuando alguien abra la cola."""
        r = client.post('/flota/odometro',
                        json={'placa': mundo['placa'], 'valor_km': 1000,
                              'origen': 'tanqueo'},
                        headers=_AUTH(mundo['t_admin']))
        assert r.status_code == 201, r.get_json()
        d = r.get_json()
        assert d['confianza'] == Confianza.DUDOSA.value
        assert 'sin foto' in d['motivo_dudosa']

    def test_el_hallazgo_ancla_una_lectura_marcada(self, client, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        r = client.post('/flota/hallazgos',
                        json={'placa': mundo['placa'], 'criticidad': 'mayor',
                              'descripcion': 'fuga de aceite', 'km': 700},
                        headers=_AUTH(mundo['t_admin']))
        assert r.status_code == 201, r.get_json()
        assert LecturaOdometro.query.one().confianza == Confianza.DUDOSA.value

    def test_la_inspeccion_diaria_tambien(self, app, db, mundo):
        """Entra por `anclar_odometro`, igual que el hallazgo — pero es un
        llamador distinto y con otro `origen`."""
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores import catalogo, inspecciones
        from flota.adaptadores.modelos import LecturaOdometro

        catalogo.sembrar(db)
        vehiculo = db.session.get(Vehiculo, mundo['veh'])
        items = inspecciones.items_del_dia_de(vehiculo, date(2026, 3, 2))
        inspecciones.registrar(
            vehiculo_id=mundo['veh'], tipo_vehiculo_obj=vehiculo, km=900,
            inspeccionada_por_usuario_id=mundo['usuario_id'],
            respuestas=[{'item_id': i.id, 'respuesta': 'optimo'} for i in items],
            segundos_llenado=120,
            # El mismo día con el que se pidió la lista: `registrar` recalcula
            # el día operativo y los ítems semanales solo entran los lunes.
            ts=datetime(2026, 3, 2, 8, 0))
        lec = LecturaOdometro.query.one()
        assert lec.origen == 'preoperacional'
        assert lec.confianza == Confianza.DUDOSA.value

    def test_el_gasto_de_campo_tambien(self, app, db, mundo):
        from flota.adaptadores import gastos
        from flota.adaptadores.modelos import LecturaOdometro

        gastos.registrar_tanqueo(
            vehiculo_id=mundo['veh'], fecha=date(2026, 3, 5), valor='168000',
            galones='12', tanque='lleno', estacion='Terpel', km=1200,
            proveedor='Terpel', origen_costo='tarjeta_convenio',
            registrado_por_usuario_id=mundo['usuario_id'])
        assert LecturaOdometro.query.one().confianza == Confianza.DUDOSA.value

    def test_ningun_escritor_escribe_la_marca_a_mano(self):
        """La política vive en UN lugar. Por AST y sobre todo `flota/`.

        Un `LecturaOdometro(..., confianza='declarada')` en un adaptador sería
        la segunda implementación de la regla —la que no mira la foto ni el
        salto— y no fallaría: escribiría una marca optimista sobre una lectura
        que el gancho habría marcado `dudosa`.
        """
        malos = []
        for ruta in sorted((_RAIZ / 'flota').rglob('*.py')):
            arbol = ast.parse(ruta.read_text(encoding='utf-8'), filename=str(ruta))
            for nodo in ast.walk(arbol):
                if not (isinstance(nodo, ast.Call)
                        and isinstance(nodo.func, ast.Name)
                        and nodo.func.id == 'LecturaOdometro'):
                    continue
                for kw in nodo.keywords:
                    if kw.arg in ('confianza', 'motivo_dudosa'):
                        malos.append(f'{ruta.name}:{nodo.lineno}')
        assert not malos, (
            f'\nEscritores que fijan la marca a mano: {malos}\n'
            'La confianza la calcula `confianza_al_nacer` en el `before_insert`. '
            'Una segunda implementación acá publica optimismo sobre una lectura '
            'que el gancho habría marcado dudosa.')

    def test_el_salto_de_x10_marca_ademas_de_la_foto_y_los_motivos_se_suman(
            self, app, db, mundo):
        """El caso THP696: 55.349 → 16.697.948 es ×301, y **entró**.

        Los dos motivos tienen que estar: quien revise necesita verlos los dos,
        y quedarse con el primero es cómo un canal de avisos se vuelve ilegible.
        """
        _lectura(db, mundo, 55_349, ts=datetime(2026, 3, 1, 8, 0))
        mala = _lectura(db, mundo, 16_697_948, ts=datetime(2026, 3, 14, 8, 0))
        assert mala.confianza == Confianza.DUDOSA.value
        assert 'sin foto' in mala.motivo_dudosa
        assert 'salto de ×' in mala.motivo_dudosa

    def test_una_lectura_normal_del_dia_siguiente_NO_acumula_el_salto(
            self, app, db, mundo):
        """La otra dirección: el detector no puede disparar sobre operación
        sana. Sin esto, «marca todo» pasaría los tests de arriba."""
        _lectura(db, mundo, 55_349, ts=datetime(2026, 3, 1, 8, 0))
        normal = _lectura(db, mundo, 55_600, ts=datetime(2026, 3, 2, 8, 0))
        assert 'salto de ×' not in normal.motivo_dudosa
        assert 'cero tiempo' not in normal.motivo_dudosa

    def test_una_lectura_SIN_ts_igual_se_juzga_contra_la_previa(
            self, app, db, mundo, monkeypatch):
        """**El `ts` lo resuelve el gancho, no el default de la columna.**

        El `default=` de la columna se aplica DESPUÉS del `before_insert`: si el
        gancho no lo resolviera, `confianza_al_nacer` compararía contra `None` y
        la regla del Δt = 0 quedaría ciega **justo en las filas que la
        necesitan** — las diez del reintento del 2026-08-03, que se escribieron
        sin `ts` explícito y con el mismo segundo.

        Se congela `utcnow` en el módulo del modelo para reproducir ese empate.
        El default de la columna capturó la función original al definirse la
        clase, así que no queda parchado: lo único que se está midiendo es lo
        que hace el gancho.
        """
        from flota.adaptadores import modelos
        from flota.adaptadores.modelos import LecturaOdometro

        instante = datetime(2026, 3, 3, 8, 0)
        _lectura(db, mundo, 1000, ts=instante)

        class _Reloj(datetime):
            @classmethod
            def utcnow(cls):
                return instante

        monkeypatch.setattr(modelos, 'datetime', _Reloj)
        fila = LecturaOdometro(vehiculo_id=mundo['veh'], valor_km=1100,
                               origen='cierre_dia',
                               autor_usuario_id=mundo['usuario_id'])
        db.session.add(fila)
        db.session.commit()
        assert 'cero tiempo' in fila.motivo_dudosa, (
            'la lectura sin `ts` no se juzgó contra la previa: 100 km en cero '
            'tiempo es velocidad infinita y tiene que quedar marcado')


# ══════════════════════════════════════════════════════════════════════════
# 2 · `verificada` no la escribe ningún automatismo
# ══════════════════════════════════════════════════════════════════════════

class TestNadieNaceVerificada:

    def test_el_gancho_levanta_si_alguien_lo_intenta_por_ORM(self, app, db, mundo):
        """La mitad legible: un error que dice a dónde ir."""
        from flota.adaptadores.modelos import LecturaOdometro

        fila = LecturaOdometro(vehiculo_id=mundo['veh'], valor_km=10,
                               ts=datetime(2026, 3, 1, 8, 0), origen='cierre_dia',
                               autor_usuario_id=mundo['usuario_id'],
                               confianza='verificada')
        db.session.add(fila)
        with pytest.raises(ValueError, match='cola de verificación'):
            db.session.commit()
        db.session.rollback()

    def test_el_trigger_lo_impide_tambien_por_SQL_crudo(self, db, mundo):
        """La mitad que no se puede esquivar. El gancho del ORM no ve un
        `INSERT` a mano —una migración, un script, psql— y esa es justamente la
        vía por la que alguien «arreglaría» 26 filas de un saque."""
        with pytest.raises(_RECHAZO, match='nace verificada'):
            db.session.execute(text(
                "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
                "origen, autor_usuario_id, confianza, verificada_por_usuario_id, "
                "verificada_ts) VALUES "
                f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
                f"{mundo['usuario_id']}, 'verificada', {mundo['usuario_id']}, "
                "'2026-03-01 08:00:00')"))
            db.session.commit()
        db.session.rollback()

    def test_y_un_INSERT_crudo_declarado_SI_entra(self, db, mundo):
        """La otra dirección: el trigger no puede bloquear una carga legítima.

        Sin este par, un trigger que abortara SIEMPRE pasaría el test de
        arriba — que es exactamente la forma de los seis guards ciegos de la
        auditoría del 2026-08-15."""
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.execute(text(
            "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
            "origen, autor_usuario_id, confianza) VALUES "
            f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
            f"{mundo['usuario_id']}, 'declarada')"))
        db.session.commit()
        assert LecturaOdometro.query.count() == 1


class TestLaFilaSoloAdmiteQueLeEscribanLaVerificacion:
    """El `UPDATE` sigue prohibido para el número. Lo que se abrió es una
    rendija con forma exacta: la confianza hacia `verificada`, y nada más."""

    def test_el_valor_km_sigue_siendo_inmutable(self, app, db, mundo):
        lec = _lectura(db, mundo, 100)
        lec.valor_km = 1
        with pytest.raises(_RECHAZO, match='append-only'):
            db.session.commit()
        db.session.rollback()

    def test_el_ts_tampoco_se_puede_mover(self, app, db, mundo):
        """Mover el `ts` reordena la serie y cambia qué lectura supersede a
        cuál después de una corrección."""
        lec = _lectura(db, mundo, 100)
        lec.ts = datetime(2027, 1, 1, 8, 0)
        with pytest.raises(_RECHAZO, match='append-only'):
            db.session.commit()
        db.session.rollback()

    def test_no_se_puede_desverificar(self, app, db, mundo):
        """Una vez verificada, no se sale. Desverificar borraría el nombre de
        quien se hizo responsable sin dejar rastro de que existió."""
        from flota.adaptadores import verificacion

        lec = _lectura(db, mundo, 100)
        verificacion.verificar(lectura_id=lec.id, usuario_id=mundo['usuario_id'])
        lec.confianza = 'declarada'
        with pytest.raises(_RECHAZO, match='append-only'):
            db.session.commit()
        db.session.rollback()

    def test_tampoco_se_puede_reescribir_el_motivo_al_verificar(self, db, mundo):
        """`motivo_dudosa` es por qué entró a la cola. Borrarlo al confirmar
        dejaría la fila sin la única explicación de qué fue lo que se miró."""
        lec = _lectura(db, mundo, 100)
        with pytest.raises(_RECHAZO, match='append-only'):
            db.session.execute(text(
                "UPDATE flota_lectura_odometro SET confianza='verificada', "
                f"verificada_por_usuario_id={mundo['usuario_id']}, "
                "verificada_ts='2026-03-02 08:00:00', motivo_dudosa=NULL "
                f"WHERE id={lec.id}"))
            db.session.commit()
        db.session.rollback()

    @pytest.mark.parametrize('columna,valor', [
        ('valor_km', '1'),
        ('ts', "'2027-01-01 08:00:00'"),
        ('vehiculo_id', '99999'),
        ('origen', "'tanqueo'"),
        ('motivo_correccion', "'lo que sea'"),
    ])
    def test_no_se_puede_colar_otro_cambio_DENTRO_de_la_verificacion(
            self, db, mundo, columna, valor):
        """**El caso que los tests de arriba no cubrían y una mutación destapó.**

        Cambiar `valor_km` solo ya estaba bloqueado… pero por la otra mitad de
        la condición (la confianza no iba hacia `verificada`), no por la lista
        de columnas inmutables. Sacar `valor_km` de esa lista dejaba los tests
        en verde: un guard verde por una razón distinta de la que afirma, que es
        la forma exacta de los seis de la auditoría del 2026-08-15.

        Lo que hay que afirmar es esto: **la rendija de la verificación no deja
        pasar nada más**. Un `UPDATE` que verifique Y de paso mueva el número
        —o el reloj, o el vehículo— tiene que rebotar entero.
        """
        lec = _lectura(db, mundo, 100)
        with pytest.raises(_RECHAZO, match='append-only'):
            db.session.execute(text(
                f"UPDATE flota_lectura_odometro SET confianza='verificada', "
                f"verificada_por_usuario_id={mundo['usuario_id']}, "
                f"verificada_ts='2026-03-02 08:00:00', {columna}={valor} "
                f"WHERE id={lec.id}"))
            db.session.commit()
        db.session.rollback()

    def test_no_se_puede_reverificar_con_otro_nombre_por_SQL_crudo(
            self, app, db, mundo):
        """El adaptador lo levanta con un mensaje legible; esto es la mitad que
        no se puede esquivar.

        La condición `OLD.confianza <> 'verificada'` es la que lo impide, y sin
        este test se podía borrar sin que nada se pusiera rojo: el otro test de
        desverificar pasa por la otra mitad de la condición.
        """
        from flota.adaptadores import verificacion

        lec = _lectura(db, mundo, 100)
        verificacion.verificar(lectura_id=lec.id, usuario_id=mundo['usuario_id'])
        with pytest.raises(_RECHAZO, match='append-only'):
            db.session.execute(text(
                "UPDATE flota_lectura_odometro SET "
                f"verificada_por_usuario_id={mundo['flota_usuario_id']}, "
                f"verificada_ts='2027-01-01 08:00:00' WHERE id={lec.id}"))
            db.session.commit()
        db.session.rollback()

    def test_y_la_verificacion_legitima_SI_pasa(self, app, db, mundo):
        """La otra dirección, y la que hace que el trigger no sea un muro:
        confirmar tiene que funcionar, o la cola no drena nunca."""
        from flota.adaptadores import verificacion

        lec = _lectura(db, mundo, 100)
        fila = verificacion.verificar(lectura_id=lec.id,
                                      usuario_id=mundo['flota_usuario_id'])
        assert fila.confianza == Confianza.VERIFICADA.value
        assert fila.verificada_por_usuario_id == mundo['flota_usuario_id']
        assert fila.verificada_ts is not None


class TestLosCHECKDeLaMarca:

    def test_una_dudosa_sin_motivo_no_entra(self, db, mundo):
        with pytest.raises(_RECHAZO):
            db.session.execute(text(
                "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
                "origen, autor_usuario_id, confianza) VALUES "
                f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
                f"{mundo['usuario_id']}, 'dudosa')"))
            db.session.commit()
        db.session.rollback()

    def test_una_declarada_CON_motivo_de_duda_tampoco(self, db, mundo):
        """Afirmaría las dos cosas a la vez: que nada la contradice y que hay
        un motivo para desconfiar."""
        with pytest.raises(_RECHAZO):
            db.session.execute(text(
                "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
                "origen, autor_usuario_id, confianza, motivo_dudosa) VALUES "
                f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
                f"{mundo['usuario_id']}, 'declarada', 'sin foto')"))
            db.session.commit()
        db.session.rollback()

    def test_un_verificador_colgado_de_una_fila_no_verificada_tampoco(
            self, db, mundo):
        """La dirección que se olvida: un nombre y una fecha de verificación
        sobre una fila que no está verificada. El health contaría trabajo que
        no afirma nada."""
        with pytest.raises(_RECHAZO):
            db.session.execute(text(
                "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
                "origen, autor_usuario_id, confianza, verificada_por_usuario_id, "
                "verificada_ts) VALUES "
                f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
                f"{mundo['usuario_id']}, 'declarada', {mundo['usuario_id']}, "
                "'2026-03-01 08:00:00')"))
            db.session.commit()
        db.session.rollback()

    def test_una_marca_inventada_no_entra(self, db, mundo):
        with pytest.raises(_RECHAZO):
            db.session.execute(text(
                "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
                "origen, autor_usuario_id, confianza) VALUES "
                f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
                f"{mundo['usuario_id']}, 'mas_o_menos')"))
            db.session.commit()
        db.session.rollback()


# ══════════════════════════════════════════════════════════════════════════
# 3 · La cola
# ══════════════════════════════════════════════════════════════════════════

class TestLaCola:

    def test_trae_las_dudosas_la_mas_vieja_primero(self, app, db, mundo):
        from flota.adaptadores import verificacion

        vieja = _lectura(db, mundo, 100, ts=datetime(2026, 3, 1, 8, 0))
        nueva = _lectura(db, mundo, 200, ts=datetime(2026, 3, 5, 8, 0))
        ids = [l.id for l, _placa in verificacion.pendientes()]
        assert ids == [vieja.id, nueva.id]

    def test_trae_la_placa_para_no_verificar_el_camion_equivocado(
            self, app, db, mundo):
        from flota.adaptadores import verificacion

        _lectura(db, mundo, 100)
        assert verificacion.pendientes()[0][1] == mundo['placa']

    def test_una_verificada_sale_de_la_cola(self, app, db, mundo):
        from flota.adaptadores import verificacion

        lec = _lectura(db, mundo, 100)
        verificacion.verificar(lectura_id=lec.id, usuario_id=mundo['usuario_id'])
        assert verificacion.pendientes() == []

    def test_una_declarada_nunca_estuvo_en_la_cola(self, db, mundo):
        """La otra dirección del filtro: si la cola trajera todo, «pendientes»
        mediría cuántas lecturas hay, no cuántas hay que mirar."""
        from flota.adaptadores import verificacion

        db.session.execute(text(
            "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
            "origen, autor_usuario_id, confianza) VALUES "
            f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
            f"{mundo['usuario_id']}, 'declarada')"))
        db.session.commit()
        assert verificacion.pendientes() == []

    def test_una_correccion_posterior_saca_de_la_cola_a_la_que_reemplazo(
            self, app, db, mundo):
        """**Lo que hace que la cola se pueda drenar.**

        Corregir no borra la fila vieja —la tabla es append-only— y tampoco la
        puede verificar nadie: es el número que se sabe malo. Sin esta regla,
        cada corrección dejaría un item eterno en la cola y la pantalla se
        abandonaría en una semana.

        La ventana la contesta el dominio, la misma que usa `validar_lectura`.
        """
        from flota.adaptadores import verificacion

        mala = _lectura(db, mundo, 16_697_948, ts=datetime(2026, 3, 1, 8, 0))
        buena = _lectura(db, mundo, 55_400, ts=datetime(2026, 3, 2, 8, 0),
                         origen='correccion', motivo='dedo en el teclado')
        ids = [l.id for l, _p in verificacion.pendientes()]
        assert mala.id not in ids, (
            'la lectura que una corrección reemplazó sigue pidiendo que alguien '
            'la confirme: nadie puede confirmar un número que ya se descartó')
        assert buena.id in ids, (
            'la corrección tampoco tiene foto: es la que ahora hay que verificar')

    def test_con_DOS_correcciones_manda_la_ultima(self, app, db, mundo):
        """La ventana empieza en la **última** corrección, no en la primera.

        Con `min()` en vez de `max()`, la corrección vieja abriría la ventana y
        la lectura mala del medio seguiría pidiendo que alguien la confirme. Con
        una sola corrección los dos criterios coinciden — por eso hace falta el
        caso de dos, que es el que los separa.
        """
        from flota.adaptadores import verificacion

        _lectura(db, mundo, 100, ts=datetime(2026, 3, 1, 8, 0))
        _lectura(db, mundo, 90, ts=datetime(2026, 3, 2, 8, 0),
                 origen='correccion', motivo='primera corrección')
        mala = _lectura(db, mundo, 16_697_948, ts=datetime(2026, 3, 3, 8, 0))
        ultima = _lectura(db, mundo, 120, ts=datetime(2026, 3, 4, 8, 0),
                          origen='correccion', motivo='segunda corrección')

        ids = [l.id for l, _p in verificacion.pendientes()]
        assert ids == [ultima.id], (
            f'la cola quedó con {ids}: la ventana tiene que empezar en la '
            f'ÚLTIMA corrección, y {mala.id} ya fue reemplazada')

    def test_sin_correcciones_no_se_esconde_nada(self, app, db, mundo):
        """La otra dirección: la ventana no puede vaciar la cola de un vehículo
        que nunca tuvo una corrección."""
        from flota.adaptadores import verificacion

        _lectura(db, mundo, 100, ts=datetime(2026, 3, 1, 8, 0))
        _lectura(db, mundo, 200, ts=datetime(2026, 3, 2, 8, 0))
        assert len(verificacion.pendientes()) == 2

    def test_verificar_dos_veces_levanta(self, app, db, mundo):
        from flota.adaptadores import verificacion

        lec = _lectura(db, mundo, 100)
        verificacion.verificar(lectura_id=lec.id, usuario_id=mundo['usuario_id'])
        with pytest.raises(verificacion.VerificacionInvalida, match='ya la verificó'):
            verificacion.verificar(lectura_id=lec.id,
                                   usuario_id=mundo['flota_usuario_id'])

    def test_verificar_una_que_nadie_puso_en_duda_levanta(self, db, mundo):
        from flota.adaptadores import verificacion
        from flota.adaptadores.modelos import LecturaOdometro

        db.session.execute(text(
            "INSERT INTO flota_lectura_odometro (vehiculo_id, valor_km, ts, "
            "origen, autor_usuario_id, confianza) VALUES "
            f"({mundo['veh']}, 10, '2026-03-01 08:00:00', 'cierre_dia', "
            f"{mundo['usuario_id']}, 'declarada')"))
        db.session.commit()
        lec = LecturaOdometro.query.one()
        with pytest.raises(verificacion.VerificacionInvalida, match='no dudosa'):
            verificacion.verificar(lectura_id=lec.id, usuario_id=mundo['usuario_id'])

    def test_verificar_una_que_no_existe_levanta(self, app, db, mundo):
        from flota.adaptadores import verificacion

        with pytest.raises(verificacion.VerificacionInvalida, match='no existe'):
            verificacion.verificar(lectura_id=98765, usuario_id=mundo['usuario_id'])


class TestLaFronteraDeLaCola:
    """Quién puede verificar, y por qué no es quien opera el turno."""

    def test_exigen_sesion(self, client):
        assert client.get('/flota/odometro/dudosas').status_code == 401
        assert client.post('/flota/odometro/1/verificar', json={}).status_code == 401

    def test_el_conductor_NO_puede_ver_ni_verificar(self, client, db, mundo):
        """Es casi siempre el autor de la lectura. Con este permiso, quien
        tecleó el número certificaría el número (regla 11)."""
        lec = _lectura(db, mundo, 100)
        assert client.get('/flota/odometro/dudosas',
                          headers=_AUTH(mundo['t_cond'])).status_code == 403
        assert client.post(f'/flota/odometro/{lec.id}/verificar', json={},
                           headers=_AUTH(mundo['t_cond'])).status_code == 403

    def test_tienda_tampoco(self, client, db, mundo):
        assert client.get('/flota/odometro/dudosas',
                          headers=_AUTH(mundo['t_tienda'])).status_code == 403

    def test_control_de_flota_SI(self, client, db, mundo):
        """El levantamiento de campo es su trabajo (FLO-PR-01): es quien tiene
        la foto y el vehículo a mano."""
        lec = _lectura(db, mundo, 100)
        d = client.get('/flota/odometro/dudosas',
                       headers=_AUTH(mundo['t_flota'])).get_json()
        assert d['total'] == 1
        assert d['pendientes'][0]['lectura_id'] == lec.id
        assert d['pendientes'][0]['placa'] == mundo['placa']
        assert d['pendientes'][0]['tiene_foto'] is False
        assert 'sin foto' in d['pendientes'][0]['motivo']

    def test_confirmar_deja_QUIEN_y_CUANDO(self, client, db, mundo):
        from flota.adaptadores.modelos import LecturaOdometro

        lec = _lectura(db, mundo, 100)
        r = client.post(f'/flota/odometro/{lec.id}/verificar', json={},
                        headers=_AUTH(mundo['t_flota']))
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d['confianza'] == 'verificada'
        assert d['verificada_por_usuario_id'] == mundo['flota_usuario_id']
        assert d['verificada_ts']

        fila = db.session.get(LecturaOdometro, lec.id)
        assert fila.verificada_por_usuario_id == mundo['flota_usuario_id']

    def test_el_verificador_sale_del_TOKEN_y_no_del_cuerpo(self, client, db, mundo):
        """Quién dice que miró la foto es el único contenido de esta marca. Si
        viniera en el JSON, se podría firmar con el nombre de otro."""
        from flota.adaptadores.modelos import LecturaOdometro

        lec = _lectura(db, mundo, 100)
        client.post(f'/flota/odometro/{lec.id}/verificar',
                    json={'verificada_por_usuario_id': mundo['cond_usuario_id'],
                          'usuario_id': mundo['cond_usuario_id']},
                    headers=_AUTH(mundo['t_flota']))
        fila = db.session.get(LecturaOdometro, lec.id)
        assert fila.verificada_por_usuario_id == mundo['flota_usuario_id']

    def test_las_dos_URL_estan_escritas_ENTERAS_en_el_PWA(self):
        """El trinquete de huérfanas no alcanza para éstas, y está medido.

        `/flota/odometro/<int:lectura_id>/verificar` se trocea en
        `/flota/odometro/` y `/verificar`, y **`/verificar` ya existe en el PWA
        por otra ruta**: `reposicion.js` tiene `/api/reposicion/verificar-stock`.
        O sea que armar la URL con `+ 'verif' + 'icar'` deja el endpoint sin
        consumidor real y el trinquete global sigue en verde — lo destapó una
        mutación de esta tanda.

        Lo que se afirma acá es la propiedad que el trinquete quiere y no puede
        medir sobre esta ruta: **la URL está escrita entera, en código y no en
        un comentario**, para que se pueda auditar leyendo el repo. Mismo motivo
        que `FLOTA_HALLAZGO_URL`.
        """
        from tests.flota.test_trinquetes_flota import _sin_comentarios

        js = _sin_comentarios(
            (_RAIZ / 'app' / 'static' / 'pwa' / 'flota.js').read_text(encoding='utf-8'))
        assert "'/flota/odometro/dudosas'" in js
        assert '`/flota/odometro/${id}/verificar`' in js, (
            'la URL de verificar dejó de estar escrita entera: una URL que solo '
            'existe en tiempo de ejecución no se puede auditar leyendo el repo')

    def test_verificar_dos_veces_devuelve_409_y_no_500(self, client, db, mundo):
        lec = _lectura(db, mundo, 100)
        client.post(f'/flota/odometro/{lec.id}/verificar', json={},
                    headers=_AUTH(mundo['t_flota']))
        r = client.post(f'/flota/odometro/{lec.id}/verificar', json={},
                        headers=_AUTH(mundo['t_flota']))
        assert r.status_code == 409
        assert 'ya la verificó' in r.get_json()['error']


# ══════════════════════════════════════════════════════════════════════════
# 4 · El ancla no puede bajar — la mitad segura del trigger hermano
# ══════════════════════════════════════════════════════════════════════════

class TestElAnclaNoBaja:
    """`km_inicial` es el ancla del sistema. Lo que este trigger impide es que
    se mueva el piso por debajo del techo, que rompería el invariante **sin dar
    error**.

    Lo que NO impide —a propósito— es que una lectura entre por debajo del
    ancla: 4 de 6 fichas de producción contradicen su propia serie (medido
    2026-09-01) y ese piso trabaría vehículos por una segunda vía. Se cuenta en
    `fichas_con_ancla_incoherente` y se decide en un mes.
    """

    @pytest.fixture
    def con_ficha(self, db, mundo):
        from flota.adaptadores.modelos import FichaTecnica

        db.session.add(FichaTecnica(vehiculo_id=mundo['veh'], posiciones_llanta=6,
                                    km_inicial=50_000,
                                    km_inicial_ts=datetime(2026, 1, 1, 8, 0)))
        db.session.commit()
        _lectura(db, mundo, 60_000)
        return mundo

    def test_bajar_el_ancla_por_debajo_del_maximo_registrado_lo_rechaza_la_BASE(
            self, app, db, con_ficha):
        from flota.adaptadores.modelos import FichaTecnica

        ficha = db.session.get(FichaTecnica, con_ficha['veh'])
        ficha.km_inicial = 40_000
        with pytest.raises(_RECHAZO, match='km_inicial'):
            db.session.commit()
        db.session.rollback()

    def test_el_endpoint_lo_devuelve_como_409_y_no_como_500(
            self, client, db, con_ficha):
        r = client.put(f"/flota/vehiculo/{con_ficha['placa']}/ficha",
                       json={'km_inicial': 40_000},
                       headers=_AUTH(con_ficha['t_flota']))
        assert r.status_code == 409, r.get_json()
        assert 'km_inicial' in r.get_json()['detalle']

    def test_subir_el_ancla_SI_se_puede(self, app, db, con_ficha):
        """La otra dirección. Un trigger que abortara todo `UPDATE` pasaría el
        test de arriba y dejaría la ficha congelada."""
        from flota.adaptadores.modelos import FichaTecnica

        ficha = db.session.get(FichaTecnica, con_ficha['veh'])
        ficha.km_inicial = 61_000
        db.session.commit()
        assert db.session.get(FichaTecnica, con_ficha['veh']).km_inicial == 61_000

    def test_bajarlo_SIN_pasar_por_debajo_del_maximo_tambien(
            self, app, db, con_ficha):
        """Corregir un ancla mal levantada de 50.000 a 55.000 es legítimo: el
        máximo registrado son 60.000 y el piso no se mueve bajo el techo."""
        from flota.adaptadores.modelos import FichaTecnica

        ficha = db.session.get(FichaTecnica, con_ficha['veh'])
        ficha.km_inicial = 55_000
        db.session.commit()
        assert db.session.get(FichaTecnica, con_ficha['veh']).km_inicial == 55_000

    def test_una_ficha_YA_incoherente_se_puede_seguir_editando(
            self, app, db, mundo):
        """**El caso de las 4 de 6, y el que decide la forma del trigger.**

        El THP696 tiene `km_inicial = 433.434` y una lectura de 16.697.948: su
        ancla ya está por debajo del máximo. Si la condición fuera solo «el
        ancla quedó por debajo del máximo», cualquier edición de esa ficha —el
        aceite, la medida de llanta— quedaría bloqueada aunque nadie tocara el
        kilometraje. Eso es trabar el vehículo por una segunda vía, que es
        exactamente lo que esta tanda decidió NO hacer.
        """
        from flota.adaptadores.modelos import FichaTecnica

        db.session.add(FichaTecnica(vehiculo_id=mundo['veh'], posiciones_llanta=6,
                                    km_inicial=433_434,
                                    km_inicial_ts=datetime(2026, 1, 1, 8, 0)))
        db.session.commit()
        _lectura(db, mundo, 16_697_948)

        ficha = db.session.get(FichaTecnica, mundo['veh'])
        ficha.medida_llanta = '7.50-16'
        db.session.commit()
        assert db.session.get(FichaTecnica, mundo['veh']).medida_llanta == '7.50-16'

    def test_un_vehiculo_sin_lecturas_no_tiene_piso_y_el_ancla_se_puede_bajar(
            self, app, db, mundo):
        """Sin serie no hay techo contra el cual comparar. Bloquear ahí sería
        inventar un piso donde no hay dato — regla 4."""
        from flota.adaptadores.modelos import FichaTecnica

        db.session.add(FichaTecnica(vehiculo_id=mundo['veh'], posiciones_llanta=6,
                                    km_inicial=50_000,
                                    km_inicial_ts=datetime(2026, 1, 1, 8, 0)))
        db.session.commit()
        ficha = db.session.get(FichaTecnica, mundo['veh'])
        ficha.km_inicial = 10
        db.session.commit()
        assert db.session.get(FichaTecnica, mundo['veh']).km_inicial == 10


# ══════════════════════════════════════════════════════════════════════════
# 5 · El CPK usa la marca — el enganche que la fase 1 dejó pedido
# ══════════════════════════════════════════════════════════════════════════

class TestElCPKNoSePublicaSobreUnTramoQueNadiePuedeRespaldar:

    @pytest.fixture
    def con_gasto(self, app, db, mundo):
        from flota.adaptadores import gastos

        gastos.registrar_gasto(
            vehiculo_id=mundo['veh'], categoria='mantenimiento',
            fecha=date(2026, 3, 10), valor='100000', proveedor='Taller',
            origen_costo='credito_proveedor', km=1000,
            registrado_por_usuario_id=mundo['usuario_id'],
            ts=datetime(2026, 3, 10, 8, 0))
        _lectura(db, mundo, 2000, ts=datetime(2026, 3, 20, 8, 0))
        return mundo

    def test_con_los_dos_extremos_dudosos_el_CPK_es_SIN_DATO(self, app, db, con_gasto):
        """**Nunca un promedio que rellena.** Un CPK con los dos kilometrajes
        en duda no tiene numerador ni denominador confiables, y publicarlo «con
        asterisco» garantiza que alguien lo va a promediar con los buenos."""
        from flota.adaptadores import gastos
        from flota.dominio.costos import SIN_DATO

        r = gastos.cpk_de(con_gasto['veh'], date(2026, 3, 1), date(2026, 3, 31))
        assert r['km'] == 1000, 'los kilómetros se siguen midiendo'
        assert r['cpk'] is SIN_DATO
        assert r['marca'] == 'sin_dato'

    def test_con_UN_extremo_dudoso_devuelve_el_numero_Y_la_marca(
            self, app, db, con_gasto):
        """Hay un extremo sólido y una resta real: el número se devuelve, pero
        va marcado. Quien lo lea decide; quien lo agregue puede excluirlo."""
        from decimal import Decimal

        from flota.adaptadores import gastos, verificacion
        from flota.adaptadores.modelos import LecturaOdometro

        alto = LecturaOdometro.query.order_by(LecturaOdometro.valor_km.desc()).first()
        verificacion.verificar(lectura_id=alto.id, usuario_id=con_gasto['usuario_id'])

        r = gastos.cpk_de(con_gasto['veh'], date(2026, 3, 1), date(2026, 3, 31))
        assert r['cpk'] == Decimal('100')
        assert r['marca'] == 'dudosa'

    def test_con_los_dos_verificados_el_numero_sale_verificado(
            self, app, db, con_gasto):
        from decimal import Decimal

        from flota.adaptadores import gastos, verificacion

        for lectura, _placa in verificacion.pendientes():
            verificacion.verificar(lectura_id=lectura.id,
                                   usuario_id=con_gasto['usuario_id'])
        r = gastos.cpk_de(con_gasto['veh'], date(2026, 3, 1), date(2026, 3, 31))
        assert r['cpk'] == Decimal('100')
        assert r['marca'] == 'verificada'

    def test_una_sola_lectura_no_es_un_tramo_declarado(self, app, db, mundo):
        """Con menos de dos lecturas no hay resta que juzgar. Publicar
        `declarada` ahí afirmaría que el tramo se pudo mirar."""
        from flota.adaptadores import gastos

        _lectura(db, mundo, 1000, ts=datetime(2026, 3, 2, 8, 0))
        r = gastos.cpk_de(mundo['veh'], date(2026, 3, 1), date(2026, 3, 31))
        assert r['marca'] == 'sin_dato'


# ══════════════════════════════════════════════════════════════════════════
# 6 · El health, que es donde esto se mira
# ══════════════════════════════════════════════════════════════════════════

class TestElHealthCuentaLaDeudaYElTrabajo:

    def test_los_dos_campos_estan_en_la_respuesta(self, client, jwt_token_admin):
        d = client.get('/flota/health',
                       headers=_AUTH(jwt_token_admin)).get_json()
        assert 'lecturas_dudosas_pendientes' in d
        assert 'lecturas_verificadas_30d' in d

    def test_la_dudosa_pendiente_se_cuenta(self, app, db, mundo):
        from flota.adaptadores.medicion import MedidorSQL

        assert MedidorSQL().lecturas_dudosas_pendientes() == 0
        _lectura(db, mundo, 100)
        assert MedidorSQL().lecturas_dudosas_pendientes() == 1

    def test_al_verificarla_baja_una_y_sube_la_otra(self, app, db, mundo):
        """Los dos campos se mueven en direcciones opuestas con el mismo gesto,
        y por eso son dos: uno es la deuda, el otro es si alguien la paga."""
        from flota.adaptadores import verificacion
        from flota.adaptadores.medicion import MedidorSQL

        lec = _lectura(db, mundo, 100)
        verificacion.verificar(lectura_id=lec.id, usuario_id=mundo['usuario_id'])
        assert MedidorSQL().lecturas_dudosas_pendientes() == 0
        assert MedidorSQL().lecturas_verificadas_30d() == 1

    def test_una_verificacion_vieja_no_cuenta_como_del_mes(self, app, db, mundo):
        """La ventana es de 30 días: si contara todo, el número diría «se ha
        verificado alguna vez» y no «se está atendiendo la cola»."""
        from flota.adaptadores import verificacion
        from flota.adaptadores.medicion import MedidorSQL

        lec = _lectura(db, mundo, 100)
        verificacion.verificar(lectura_id=lec.id, usuario_id=mundo['usuario_id'],
                               ts=datetime.utcnow() - timedelta(days=60))
        assert MedidorSQL().lecturas_verificadas_30d() == 0

    def test_la_cuenta_del_health_es_la_MISMA_lista_que_muestra_la_pantalla(
            self, app, db, mundo):
        """Regla 0: si el health contara `WHERE confianza='dudosa'` por su
        cuenta, diría un número y la pantalla mostraría otra lista — y no habría
        forma de saber cuál creer. El caso que los separa es la corrección."""
        from flota.adaptadores import verificacion
        from flota.adaptadores.medicion import MedidorSQL

        _lectura(db, mundo, 16_697_948, ts=datetime(2026, 3, 1, 8, 0))
        _lectura(db, mundo, 55_400, ts=datetime(2026, 3, 2, 8, 0),
                 origen='correccion', motivo='dedo en el teclado')
        assert (MedidorSQL().lecturas_dudosas_pendientes()
                == len(verificacion.pendientes()) == 1)
