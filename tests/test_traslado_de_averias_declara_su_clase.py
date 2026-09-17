"""El traslado dice qué clase de mercancía mueve, y la base lo hace cumplir.

Tres frentes, en este orden:

  1. `es_averia()` es la ÚNICA implementación de la pregunta — el criterio no
     se copia, se consulta.
  2. Los dos CHECK muerden en la dirección mala Y dejan pasar la operación
     sana. Un detector que solo se prueba en una dirección prueba la mitad
     (memoria: `detector-en-las-dos-direcciones`).
  3. La migración `m026` crea de verdad las siete columnas nuevas.

El punto 3 no es ceremonia. `tests/test_deriva_esquema.py` busca el nombre de
cada columna como palabra suelta en el texto de TODAS las migraciones, sin
distinguir tabla — y `motivo_averia` **ya existe** en `m025` sobre
`items_recepcion`. O sea: si la migración de este cambio no existiera, el
detector daría verde igual. Y como la suite arma el esquema con
`create_all()` (`tests/conftest.py:67`), ninguna migración se ejercita: la
suite entera pasaría, el deploy pasaría, y producción se quedaría sin las
columnas. Este archivo es el que tapa ese hueco para estas siete.
"""
import re
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db as _db
from app.models.traslado import (ClaseTraslado, EstadoTraslado,
                                 ItemSolicitudTraslado, SolicitudTraslado)
from app.models.usuario import Usuario


MIGRACION = (Path(__file__).resolve().parents[1]
             / 'migrations' / 'versions' / 'm026traslado_de_averias.py')

#: Las columnas que este cambio agrega, por tabla. Si alguien agrega una
#: octava al modelo y no a la migración, `test_la_migracion_crea_las_columnas`
#: no la ve — pero `test_el_modelo_no_tiene_columnas_de_averia_sin_declarar`
#: sí, porque compara contra el modelo vivo.
COLUMNAS_NUEVAS = {
    'solicitudes_traslado': (
        'clase_traslado', 'averia_evidencia', 'averia_veredicto',
        'averia_veredicto_por', 'averia_veredicto_at', 'averia_veredicto_nota',
    ),
    'items_solicitud_traslado': ('motivo_averia',),
}


@pytest.fixture
def usuario(db):
    u = Usuario(nombre='Jefe Punto', email='jefe.punto@test.co', rol='admin')
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _solicitud(usuario, codigo, **extra):
    return SolicitudTraslado(
        codigo=codigo,
        bodega_origen_siesa='NC1',
        bodega_destino_siesa='NB1',
        estado=EstadoTraslado.BORRADOR,
        solicitante_id=usuario.id,
        **extra,
    )


# ── 1 · una política, una función ──────────────────────────────────────────

class TestUnaSolaRespuesta:

    def test_es_averia_responde_por_la_clase(self, db, usuario):
        normal = _solicitud(usuario, 'T-N1')
        averia = _solicitud(usuario, 'T-A1', clase_traslado=ClaseTraslado.AVERIAS)
        db.session.add_all([normal, averia])
        db.session.commit()

        assert normal.es_averia() is False
        assert averia.es_averia() is True

    def test_el_default_es_normal(self, db, usuario):
        """Los 174 traslados que ya existen son todos NORMAL, y un traslado
        nuevo que no diga nada también: la clase que no se declara no puede
        significar «avería»."""
        s = _solicitud(usuario, 'T-N2')
        db.session.add(s)
        db.session.commit()
        assert s.clase_traslado == ClaseTraslado.NORMAL
        assert s.es_averia() is False

    def test_nadie_compara_la_clase_con_un_literal_suelto(self):
        """Trinquete de «una política, una función».

        El criterio «¿esto es una avería?» vive en `SolicitudTraslado.es_averia`
        y en ningún otro lado. Si un servicio escribe
        `if s.clase_traslado == 'AVERIAS'` a mano, el día que la clase se decida
        por otra cosa quedan dos criterios y uno se olvida. Es literalmente el
        defecto que costó 25× de sobrecompra en este repo.
        """
        raiz = Path(__file__).resolve().parents[1]
        infractores = []
        patron = re.compile(r"clase_traslado\s*[!=]=\s*['\"]")
        for py in list((raiz / 'app').rglob('*.py')):
            if py.name == 'traslado.py' and py.parent.name == 'models':
                continue  # la definición misma
            for n, linea in enumerate(py.read_text(encoding='utf-8').splitlines(), 1):
                if patron.search(linea):
                    infractores.append(f'{py.relative_to(raiz)}:{n}')
        assert not infractores, (
            'Comparación literal contra `clase_traslado` fuera del modelo. '
            'Usá `solicitud.es_averia()`:\n  ' + '\n  '.join(infractores))


# ── 2 · el CHECK, en las dos direcciones ───────────────────────────────────

class TestLaBaseLoHaceCumplir:

    @pytest.mark.parametrize('clase', ['CUALQUIERA', 'averias', 'AVERIA', ''])
    def test_una_clase_desconocida_no_entra(self, db, usuario, clase):
        """Una clase desconocida no daría error en el código: se comportaría
        como NORMAL en todos los `if`, y un traslado de averías se ubicaría
        como mercancía vendible. Por eso lo ataja la base."""
        db.session.add(_solicitud(usuario, f'T-X-{clase or "vacio"}',
                                  clase_traslado=clase))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    @pytest.mark.parametrize('campo,valor', [
        ('averia_veredicto', True),
        ('averia_veredicto', False),
        ('averia_evidencia', 'lo revisé y estaba roto'),
        ('averia_veredicto_nota', 'confirmado'),
    ])
    def test_un_traslado_normal_no_puede_cargar_veredicto(self, db, usuario,
                                                          campo, valor):
        """Si estos campos se escriben sobre un traslado normal, ese traslado
        empieza a comportarse como uno de averías y el stock vendible
        desaparece del pool sin que nadie lo haya pedido."""
        db.session.add(_solicitud(usuario, f'T-V-{campo}-{valor}',
                                  clase_traslado=ClaseTraslado.NORMAL,
                                  **{campo: valor}))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_un_traslado_normal_limpio_pasa(self, db, usuario):
        """La otra mitad del detector: el guard no puede estorbar la operación
        sana. Los 174 traslados de producción son exactamente esta forma."""
        db.session.add(_solicitud(usuario, 'T-OK-1'))
        db.session.commit()
        assert SolicitudTraslado.query.filter_by(codigo='T-OK-1').one()

    def test_una_averia_sin_veredicto_pasa_y_es_el_estado_inicial(self, db, usuario):
        """`None` no es «no»: es «nadie de NB1 miró todavía», y es como nace
        todo traslado de averías."""
        s = _solicitud(usuario, 'T-OK-2', clase_traslado=ClaseTraslado.AVERIAS)
        db.session.add(s)
        db.session.commit()
        assert s.averia_veredicto is None
        assert s.es_averia() is True

    @pytest.mark.parametrize('veredicto', [True, False])
    def test_una_averia_dictaminada_pasa_en_los_dos_sentidos(self, db, usuario,
                                                             veredicto):
        """`False` («no estaba averiada, vuelve a vendible») tiene que poder
        escribirse igual que `True`. Un guard que solo dejara confirmar
        obligaría a mentir para poder devolver la mercancía."""
        from datetime import datetime
        s = _solicitud(usuario, f'T-OK-3-{veredicto}',
                       clase_traslado=ClaseTraslado.AVERIAS,
                       averia_evidencia='fotos del jefe del punto',
                       averia_veredicto=veredicto,
                       averia_veredicto_por=usuario.id,
                       averia_veredicto_at=datetime.utcnow(),
                       averia_veredicto_nota='revisado en muelle')
        db.session.add(s)
        db.session.commit()
        assert s.averia_veredicto is veredicto


# ── 3 · la migración existe de verdad ──────────────────────────────────────

class TestLaMigracionNoSeOlvido:
    """El detector de deriva de esquema no puede ver este cambio (ver el
    docstring del módulo). Esto lo suple."""

    def test_el_archivo_existe_y_encadena_desde_m025(self):
        assert MIGRACION.exists(), f'falta {MIGRACION.name}'
        texto = MIGRACION.read_text(encoding='utf-8')
        assert "revision = 'm026trasladoaverias'" in texto
        assert "down_revision = 'm025averiaenrecepcion'" in texto

    @pytest.mark.parametrize('tabla,columna', [
        (t, c) for t, cols in COLUMNAS_NUEVAS.items() for c in cols
    ])
    def test_la_migracion_crea_la_columna(self, tabla, columna):
        texto = MIGRACION.read_text(encoding='utf-8')
        # El batch de la tabla, hasta el siguiente batch o el fin del upgrade.
        bloque = texto.split(f"batch_alter_table('{tabla}')", 1)
        assert len(bloque) == 2, f'la migración no toca {tabla}'
        cuerpo = re.split(r"batch_alter_table\(|^def downgrade", bloque[1],
                          maxsplit=1, flags=re.M)[0]
        assert f"'{columna}'" in cuerpo, (
            f'{columna} está en el modelo pero la migración no la agrega a {tabla}')

    def test_el_modelo_no_tiene_columnas_de_averia_sin_declarar(self):
        """Si alguien agrega una octava columna de avería al modelo, esta lista
        queda corta y el test de arriba no la mira. Esto lo delata."""
        declaradas = {
            'solicitudes_traslado': {
                c.name for c in SolicitudTraslado.__table__.columns
                if c.name.startswith('averia_') or c.name == 'clase_traslado'},
            'items_solicitud_traslado': {
                c.name for c in ItemSolicitudTraslado.__table__.columns
                if 'averia' in c.name},
        }
        for tabla, esperadas in COLUMNAS_NUEVAS.items():
            assert declaradas[tabla] == set(esperadas), (
                f'{tabla}: el modelo tiene {sorted(declaradas[tabla])} y '
                f'COLUMNAS_NUEVAS dice {sorted(esperadas)}. Actualizá la lista '
                f'Y la migración.')

    def test_el_check_acepta_exactamente_las_clases_declaradas(self):
        """El CHECK y `ClaseTraslado.TODAS` son dos fuentes del mismo hecho.

        Sin este trinquete, ensanchar el CHECK con un tercer valor no rompe
        nada: los tests de rechazo prueban valores concretos ('CUALQUIERA',
        'AVERIA', '') que un CHECK más ancho sigue rechazando igual. Es la
        forma exacta en que una lista de permitidos crece por el borde que
        nadie mira — el repo ya pagó esa factura con una tupla de roles.

        Detectado por mutación: agregar `'X'` a la lista del CHECK dejaba la
        suite entera en verde.
        """
        check = next(c for c in SolicitudTraslado.__table__.constraints
                     if getattr(c, 'name', None) == 'ck_traslado_clase_conocida')
        aceptados = set(re.findall(r"'([^']*)'", str(check.sqltext)))
        assert aceptados == set(ClaseTraslado.TODAS), (
            f'El CHECK acepta {sorted(aceptados)} y ClaseTraslado.TODAS dice '
            f'{sorted(ClaseTraslado.TODAS)}. Si se agrega una clase, va en los '
            f'dos lados Y en la migración.')

    def test_la_migracion_declara_las_mismas_clases_que_el_modelo(self):
        """Y la tercera fuente: la migración. En producción manda ella."""
        texto = MIGRACION.read_text(encoding='utf-8')
        linea = next(l for l in texto.splitlines()
                     if 'clase_traslado IN (' in l)
        aceptados = set(re.findall(r"'([^']*)'", linea))
        assert aceptados == set(ClaseTraslado.TODAS), (
            f'La migración acepta {sorted(aceptados)} y el modelo '
            f'{sorted(ClaseTraslado.TODAS)}.')

    def test_ninguna_clase_colisiona_con_un_literal_de_zona(self):
        """El homónimo que causó esto.

        `'AVERIAS'` es el `tipo_zona` con el que se decide si algo se puede
        vender. Una clase de traslado con ese mismo valor son dos columnas
        indistinguibles en un dump — y el día que alguien las compare, la
        clase de un documento decide si una ubicación es vendible.
        """
        from app.services.picking_service import ZONA_AVERIAS
        prohibidos = {ZONA_AVERIAS, 'CUARENTENA', 'cuarentena', 'GENERAL'}
        colisiones = set(ClaseTraslado.TODAS) & prohibidos
        assert not colisiones, (
            f'{colisiones} ya significa otra cosa en esta base (tipo_zona). '
            f'Elegí un valor que no se pueda confundir.')

    def test_los_dos_check_van_tambien_en_el_modelo(self):
        """SQLite no aplica un CHECK agregado por ALTER; la suite arma el
        esquema con `create_all()`. Si el CHECK vive solo en la migración,
        ningún test de este archivo lo ejercita y el guard queda sin probar
        hasta producción."""
        nombres = {c.name for c in SolicitudTraslado.__table__.constraints
                   if c.__class__.__name__ == 'CheckConstraint'}
        assert 'ck_traslado_clase_conocida' in nombres
        assert 'ck_traslado_veredicto_solo_en_averias' in nombres


# ── 4 · el ítem ────────────────────────────────────────────────────────────

class TestElItemLlevaElMotivo:

    def test_el_motivo_viaja_por_linea(self, db, usuario, producto):
        s = _solicitud(usuario, 'T-M1', clase_traslado=ClaseTraslado.AVERIAS)
        db.session.add(s)
        db.session.flush()
        it = ItemSolicitudTraslado(
            solicitud_id=s.id, producto_id=producto.id,
            cantidad_solicitada=3, motivo_averia='empaque roto en bodega')
        db.session.add(it)
        db.session.commit()
        assert it.to_dict()['motivo_averia'] == 'empaque roto en bodega'

    def test_el_item_no_duplica_la_cantidad_averiada(self):
        """Decisión deliberada, no olvido.

        En un traslado de averías la mercancía averiada es el documento
        entero: la cantidad la dice `cantidad_solicitada` y la cadena
        `solicitada ≥ aprobada ≥ enviada ≥ recibida` la recorta paso a paso.
        Una `cantidad_averiada` acá sería una segunda fuente de verdad para el
        mismo número, sin nada que la mantenga sincronizada con la primera.

        (En `ItemRecepcion` sí existe, y ahí es correcto: en una recepción por
        OC la avería es un subconjunto de lo recibido.)
        """
        cols = {c.name for c in ItemSolicitudTraslado.__table__.columns}
        assert 'cantidad_averiada' not in cols, (
            'Si esto se agrega, hay que decir qué la mantiene coherente con '
            'cantidad_solicitada — y agregar el CHECK que lo garantice.')


# ── 5 · el payload no cambia de forma para nadie ───────────────────────────

def test_el_dict_de_un_traslado_normal_no_miente(db, usuario):
    """El PWA lee ~19 claves de este dict con nombres a mano. Las claves
    nuevas se agregan; ninguna existente cambia de nombre ni de tipo, y en un
    traslado normal las de avería son todas `None`/`False` — no `''`, que un
    `if` del front leería como «hay algo escrito»."""
    s = _solicitud(usuario, 'T-D1')
    db.session.add(s)
    db.session.commit()
    d = s.to_dict()
    assert d['clase_traslado'] == ClaseTraslado.NORMAL
    assert d['es_averia'] is False
    for k in ('averia_evidencia', 'averia_veredicto', 'averia_veredicto_por',
              'averia_veredicto_nombre', 'averia_veredicto_at',
              'averia_veredicto_nota'):
        assert d[k] is None, f'{k} salió {d[k]!r} en vez de None'
    # Las que el front ya usaba siguen ahí.
    for k in ('id', 'codigo', 'estado', 'bodega_origen_siesa',
              'bodega_destino_siesa', 'items', 'total_items'):
        assert k in d
