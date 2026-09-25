"""«¿Puede salir este camión?» — UNA política, tres pantallas (2026-09-24).

El rediseño de flota lo hicieron cinco frentes en paralelo y cada uno contestó
la pregunta por su lado: el semáforo de la bandeja (`senales.semaforo`), las
advertencias al despachar (`senales_ruta.advertencias_de_flota`) y el aviso del
conductor (`flotaCondAvisos`, que decidía el color en el teléfono y lo llamaba
«amarillo»). El despacho no sumaba el daño bloqueante ni el preventivo vencido;
el semáforo no sabía de la orden de taller; los tres escribían distinto el mismo
SOAT vencido.

La clase: **un nivel (rojo/ámbar/verde) de un vehículo decidido fuera de la
política**. Este archivo tiene:

1. la política, caso por caso (`flota/dominio/salida.py`);
2. las tres pantallas contra UN mundo: los mismos motivos y los mismos textos;
3. el trinquete: por AST, nadie en `flota/` ni en `app/` escribe `'rojo'`,
   `'ambar'`, `'amarillo'` ni `'verde'` ni usa `ROJO`/`AMBAR`/`VERDE` fuera de
   la política; en el JS de flota, ningún nivel nace en el teléfono. Con
   meta-tests (lo que debe ver, lo que no, piso mínimo).
"""
import ast
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from flota.dominio import salida as dom
from tests.flota.test_bandeja import HOY, _bandeja, _fila, mundo  # noqa: F401

RAIZ = Path(__file__).resolve().parents[2]
D = date(2026, 9, 24)


def _hechos(**kw):
    """Un vehículo sano: todo vigente, sin daños, apto, con turno, ficha completa."""
    base = dict(
        hoy=D,
        papeles=tuple(dom.Papel(t, dom.VIGENTE, D + timedelta(days=200))
                      for t in dom.NOMBRE_PAPEL),
        danos_bloqueantes=0, danos_vencidos=0, danos_en_plazo=0,
        inspeccion='apta', sale_hoy=False, preventivo_vencidas=(),
        preventivo_por_vencer=0, ot_abiertas=0, km_conocido=True, km_dudoso=False,
        custodia='conductor', custodio_conductor_id=7, conductor_de_la_ruta=None,
        fuera_de_sede=False, ficha='completa', ficha_falta=())
    base.update(kw)
    return dom.Hechos(**base)


def _papeles(**estados):
    """Papeles vigentes salvo los que se nombran: `soat=(estado, vence)`."""
    out = []
    for t in dom.NOMBRE_PAPEL:
        if t in estados:
            estado, vence = estados[t]
            out.append(dom.Papel(t, estado, vence))
        else:
            out.append(dom.Papel(t, dom.VIGENTE, D + timedelta(days=200)))
    return tuple(out)


def _claves(ev):
    return [m.clave for m in ev.motivos]


# ═════════════════════════════════════════════════════════════════════════
# 1 · La política
# ═════════════════════════════════════════════════════════════════════════

class TestLaPolitica:

    def test_verde_dice_que_es_lo_conocido(self):
        ev = dom.evaluar(_hechos())
        assert ev.color == dom.VERDE
        assert ev.semaforo()['porque'][0]['texto'] == 'Sin pendientes conocidos'

    def test_el_soat_vencido_tiene_un_solo_texto(self):
        ev = dom.evaluar(_hechos(papeles=_papeles(soat=(dom.VENCIDO, date(2026, 9, 19)))))
        m = ev.motivos[0]
        assert (m.clave, m.nivel, m.frena) == ('soat_vencido', dom.ROJO, True)
        assert m.texto == 'SOAT vencido desde 19/09/2026 (hace 5 días)'
        assert m.corto == 'SOAT vencido'

    def test_singular_genero_y_hoy(self):
        rtm = dom.evaluar(_hechos(papeles=_papeles(
            rtm=(dom.VENCIDO, D - timedelta(days=1))))).motivos[0]
        assert rtm.texto == 'Revisión técnico-mecánica vencida desde 23/09/2026 (hace 1 día)'
        pv = dom.evaluar(_hechos(papeles=_papeles(soat=(dom.POR_VENCER, D)))).motivos[0]
        assert pv.texto == 'SOAT vence el 24/09/2026 (hoy)'
        ne = dom.evaluar(_hechos(papeles=_papeles(
            poliza_rc=(dom.NO_ENCONTRADO, None)))).motivos[0]
        assert ne.texto.startswith('Póliza de responsabilidad civil: nadie la pudo mostrar')

    @pytest.mark.parametrize('kw,clave', [
        ({'danos_bloqueantes': 1}, 'dano_bloqueante'),
        ({'danos_vencidos': 2}, 'dano_vencido'),
        ({'inspeccion': 'no_apta'}, 'inspeccion_no_apta'),
        ({'sale_hoy': True, 'inspeccion': 'incompleta'}, 'inspeccion_incompleta'),
        ({'sale_hoy': True, 'inspeccion': 'sin_hacer'}, 'sin_inspeccion_hoy'),
        ({'preventivo_vencidas': (dom.TareaVencida('aceite de motor', 600),)},
         'preventivo_vencido'),
    ])
    def test_cada_rojo_frena_la_salida(self, kw, clave):
        ev = dom.evaluar(_hechos(**kw))
        assert ev.color == dom.ROJO
        m = next(m for m in ev.motivos if m.clave == clave)
        assert m.nivel == dom.ROJO and m.frena and m.ambito == dom.SALIDA

    @pytest.mark.parametrize('kw,clave,frena', [
        ({'papeles': _papeles(soat=(dom.POR_VENCER, D + timedelta(days=10)))},
         'soat_por_vencer', False),
        ({'papeles': _papeles(soat=(dom.SIN_CARGAR, None))}, 'soat_sin_registro', True),
        ({'papeles': _papeles(tarjeta_propiedad=(dom.SIN_CARGAR, None))},
         'tarjeta_propiedad_sin_registro', False),
        ({'danos_en_plazo': 1}, 'dano_en_plazo', False),
        ({'ot_abiertas': 1}, 'en_taller', True),
        ({'preventivo_por_vencer': 2}, 'preventivo_por_vencer', False),
        ({'preventivo_vencidas': None}, 'preventivo_sin_dato', False),
        ({'inspeccion': 'sin_dato'}, 'inspeccion_sin_dato', True),
        ({'km_conocido': False}, 'km_sin_dato', False),
        ({'km_dudoso': True}, 'km_en_duda', False),
        ({'custodia': 'sin_turno', 'custodio_conductor_id': None}, 'sin_custodia', True),
        ({'custodia': 'pendiente_sede'}, 'custodia_pendiente_sede', False),
        ({'fuera_de_sede': True}, 'fuera_de_sede', False),
        ({'ficha': 'sin_ficha'}, 'sin_ficha', False),
        ({'ficha': 'incompleta', 'ficha_falta': ('distribucion',)}, 'ficha_incompleta', False),
    ])
    def test_cada_ambar(self, kw, clave, frena):
        ev = dom.evaluar(_hechos(**kw))
        assert ev.color == dom.AMBAR, _claves(ev)
        m = next(m for m in ev.motivos if m.clave == clave)
        assert m.nivel == dom.AMBAR and m.frena is frena

    def test_la_orden_de_taller_no_la_sabia_el_semaforo(self):
        """El semáforo viejo no sumaba la OT abierta; el despacho sí. Ahora
        está en los dos y el conductor también lo ve."""
        ev = dom.evaluar(_hechos(ot_abiertas=2))
        assert [m.texto for m in ev.para_el_conductor()] == [
            'Tiene 2 órdenes de taller abiertas']
        assert [m.clave for m in ev.para_despachar()] == ['en_taller']

    def test_sin_ruta_no_exige_inspeccion(self):
        assert dom.evaluar(_hechos(inspeccion='sin_hacer')).color == dom.VERDE

    def test_con_ruta_hoy_y_apta_no_es_rojo(self):
        assert dom.evaluar(_hechos(sale_hoy=True, inspeccion='apta')).color == dom.VERDE

    def test_el_despacho_compara_el_turno_con_la_ruta_y_la_bandeja_no(self):
        """La bandeja no pasa conductor de la ruta: eso es la señal
        `turno_de_la_ruta`, con contexto. El despacho sí."""
        assert dom.evaluar(_hechos(custodio_conductor_id=7)).color == dom.VERDE
        ev = dom.evaluar(_hechos(custodio_conductor_id=7, conductor_de_la_ruta=8))
        assert _claves(ev) == ['custodio_distinto']
        ev = dom.evaluar(_hechos(custodia='sede', custodio_conductor_id=None,
                                 conductor_de_la_ruta=8))
        assert _claves(ev) == ['custodia_en_sede']

    def test_la_ficha_dice_que_detector_queda_ciego(self):
        ev = dom.evaluar(_hechos(ficha='incompleta', ficha_falta=('capacidad_tanque',)))
        assert ev.motivos[0].texto == (
            'Ficha técnica incompleta: falta capacidad del tanque (sin ella no '
            'se detecta un tanqueo que no cabe en el tanque)')

    def test_lo_mas_grave_primero(self):
        ev = dom.evaluar(_hechos(danos_en_plazo=1, km_dudoso=True,
                                 papeles=_papeles(soat=(dom.VENCIDO, D - timedelta(days=3)))))
        assert [m.nivel for m in ev.motivos][0] == dom.ROJO
        assert _claves(ev)[0] == 'soat_vencido'

    def test_el_conductor_ve_lo_de_salir_y_no_la_gestion(self):
        ev = dom.evaluar(_hechos(danos_en_plazo=1, km_dudoso=True, ficha='sin_ficha',
                                 custodia='sin_turno', custodio_conductor_id=None))
        assert [m.clave for m in ev.para_el_conductor()] == ['dano_en_plazo']
        assert ev.color_para_el_conductor() == dom.AMBAR
        aviso = ev.aviso_del_conductor()
        assert aviso == {'color': 'ambar', 'motivos': [
            {'nivel': 'ambar', 'texto': '1 daño abierto dentro de plazo',
             'corto': '1 daño abierto', 'clave': 'dano_en_plazo', 'frena': False}]}

    def test_los_papeles_sin_cargar_van_en_un_renglon(self):
        ev = dom.evaluar(_hechos(papeles=tuple(dom.Papel(t, dom.SIN_CARGAR)
                                               for t in dom.NOMBRE_PAPEL)))
        porque = ev.semaforo()['porque']
        assert len(porque) == 1
        assert porque[0]['texto'] == (
            'Sin cargar, no se sabe si están al día: SOAT, revisión '
            'técnico-mecánica, póliza de responsabilidad civil, tarjeta de propiedad')
        assert porque[0]['frena'] is True       # dos de ellos son para circular
        # Pero el despacho ve las claves de a una: el FORZAR las reconoce así.
        assert [m.clave for m in ev.para_despachar()] == [
            'soat_sin_registro', 'rtm_sin_registro']

    def test_un_nivel_desconocido_revienta(self):
        with pytest.raises(KeyError):
            dom.orden_de_nivel('amarillo')

    def test_ninguna_firma_recibe_una_persona(self):
        """Regla 2: la política compara ids de turno; nunca un nombre."""
        import dataclasses
        for f in dataclasses.fields(dom.Hechos):
            assert 'nombre' not in f.name and 'usuario' not in f.name, f.name


# ═════════════════════════════════════════════════════════════════════════
# 2 · Tres pantallas, una respuesta — contra el mismo mundo
# ═════════════════════════════════════════════════════════════════════════

class TestTresPantallasUnaRespuesta:
    """ROJ001 del mundo de la bandeja: SOAT vencido hace 5 días, ruta de hoy
    en la calle sin inspección, preventivo de aceite vencido. Se le agrega un
    daño bloqueante y una orden de taller abierta — las dos cosas que el
    despacho o el semáforo no sabían."""

    @pytest.fixture
    def roj(self, db, mundo):  # noqa: F811
        from app.models.usuario import Usuario
        from flota.adaptadores import hallazgos, taller
        vid = mundo['veh']['ROJ001']
        admin = Usuario.query.filter_by(email='jefa@bandeja.test').one()
        hallazgos.reportar(vehiculo_id=vid, criticidad='bloqueante',
                           descripcion='freno de mano no sostiene', km=25_600,
                           reportado_por_usuario_id=admin.id)
        taller.abrir(vehiculo_id=vid, tipo='correctiva', taller='Taller Neiva',
                     descripcion='revisar frenos', km=25_600,
                     abierta_por_usuario_id=admin.id)
        return vid

    def _salida_de_las_tres(self, client, mundo, vid):
        from app.models.ruta_despacho import RutaDespacho
        from app.services import senales_ruta as sr
        from flota.api.conductor import _estado_del_vehiculo

        fila = _fila(_bandeja(client, mundo), 'ROJ001')
        bandeja = {p['clave']: p['texto'] for p in fila['semaforo']['porque']}
        ruta = RutaDespacho.query.filter_by(vehiculo_id=vid,
                                            fecha_programada=HOY).one()
        despacho = {a['clave']: a['texto'] for a in sr.advertencias_de_flota(ruta)}
        conductor = {m['clave']: m['texto']
                     for m in _estado_del_vehiculo(vid)['salida']['motivos']}
        return fila, bandeja, despacho, conductor

    def test_los_tres_dicen_lo_mismo_con_las_mismas_palabras(self, client, mundo, roj):
        fila, bandeja, despacho, conductor = self._salida_de_las_tres(client, mundo, roj)
        soat = (f'SOAT vencido desde {(HOY - timedelta(days=5)).strftime("%d/%m/%Y")}'
                ' (hace 5 días)')
        for clave, texto in (('soat_vencido', soat),
                             ('dano_bloqueante', '1 daño bloqueante abierto'),
                             ('en_taller', 'Tiene una orden de taller abierta'),
                             ('sin_inspeccion_hoy',
                              'Tiene ruta hoy y todavía no tiene la inspección de hoy'),
                             ('preventivo_vencido',
                              'Mantenimiento vencido: aceite de motor (600 km pasado)')):
            assert bandeja[clave] == texto, ('bandeja', clave, bandeja)
            assert despacho[clave] == texto, ('despacho', clave, despacho)
            assert conductor[clave] == texto, ('conductor', clave, conductor)
        assert fila['semaforo']['color'] == 'rojo'

    def test_el_despacho_ahora_suma_el_dano_bloqueante_y_el_preventivo(self, client,
                                                                       mundo, roj):
        _f, _b, despacho, _c = self._salida_de_las_tres(client, mundo, roj)
        assert {'dano_bloqueante', 'preventivo_vencido'} <= set(despacho)
        # Y compara el turno con la ruta: ROJ001 lo tiene Carla y sale con Beto.
        assert 'custodio_distinto' in despacho

    def test_lo_que_el_conductor_ve_es_lo_de_salir_de_la_bandeja(self, client, mundo, roj):
        _f, bandeja, _d, conductor = self._salida_de_las_tres(client, mundo, roj)
        assert set(conductor) <= set(bandeja)
        assert not {'km_en_duda', 'sin_ficha', 'ficha_incompleta',
                    'sin_custodia'} & set(conductor)

    def test_el_endpoint_del_conductor_trae_la_salida(self, client, mundo, roj):
        """`GET /flota/conductor/mi-turno` publica `estado_vehiculo.salida` con
        el color decidido en el servidor."""
        from flask_jwt_extended import create_access_token

        from app.models.conductor import Conductor
        from app.models.usuario import Usuario
        from app.extensions import db
        u = Usuario(email='carla@bandeja.test', nombre='Carla', rol='conductor',
                    activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.flush()
        Conductor.query.filter_by(nombre='Carla').one().usuario_id = u.id
        db.session.commit()
        r = client.get('/flota/conductor/mi-turno', headers={
            'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'})
        assert r.status_code == 200, r.get_json()
        s = r.get_json()['estado_vehiculo']['salida']
        assert s['color'] == 'rojo'
        assert 'dano_bloqueante' in {m['clave'] for m in s['motivos']}


# ═════════════════════════════════════════════════════════════════════════
# 3 · El trinquete: nadie más decide el nivel de un vehículo
# ═════════════════════════════════════════════════════════════════════════

NIVELES_LITERALES = {'rojo', 'ambar', 'amarillo', 'verde'}
NOMBRES_DE_NIVEL = {'ROJO', 'AMBAR', 'VERDE'}
LA_POLITICA = RAIZ / 'flota' / 'dominio' / 'salida.py'

#: Sitios fuera de la política que nombran un nivel, con su motivo. Solo encoge.
INVENTARIO_PY: dict = {}

#: OTROS semáforos, que no son el de un vehículo y usan las mismas palabras.
#: Cada uno es la única función de SU pregunta (Regla 0: una política, una
#: función), y el trinquete los exime solo en ese ámbito —la función o la
#: constante de módulo—, no el archivo entero: un nivel de vehículo decidido
#: en otra función del mismo archivo sigue rojo. Llegaron en la integración del
#: 2026-09-24 (analítica «¿cómo vamos?» y «solo lo actual»), escritos en
#: paralelo a este trinquete. Una entrada nueva es una decisión: `test_otros_
#: semaforos_no_crecen` la frena.
OTROS_SEMAFOROS = {
    ('app/services/analitica_kpi.py', 'semaforo'):
        'Semáforo de una métrica contra su META (portada de analítica); no mira vehículos',
    ('app/services/analitica_kpi.py', 'NIVELES_SEMAFORO'):
        'El vocabulario de ese mismo semáforo de metas',
    ('app/services/analitica_kpi.py', 'TEXTO_SEMAFORO'):
        'Las palabras de ese mismo semáforo de metas',
    ('app/services/dashboard_service.py', 'semaforo_de_conteo'):
        'Semáforo de la cola de conteo cíclico del tablero; no mira vehículos',
}


def _violaciones_py(fuente: str):
    """Constantes de texto que SON un nivel, y usos de ROJO/AMBAR/VERDE.

    Un docstring que menciona «rojo» no cuenta (la constante no es igual a la
    palabra); un `from … import ROJO` tampoco (no es un uso: el trinquete de
    re-exportación lo ve el uso)."""
    out = []
    for n in ast.walk(ast.parse(fuente)):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and n.value in NIVELES_LITERALES:
            out.append((n.lineno, repr(n.value)))
        elif isinstance(n, ast.Name) and n.id in NOMBRES_DE_NIVEL \
                and isinstance(n.ctx, ast.Load):
            out.append((n.lineno, n.id))
        elif isinstance(n, ast.Attribute) and n.attr in NOMBRES_DE_NIVEL:
            out.append((n.lineno, n.attr))
    return out


def _violaciones_con_ambito(fuente: str):
    """Como `_violaciones_py`, con el ámbito de cada una: la función de módulo
    que la contiene o, fuera de toda función, el nombre de la constante de
    módulo que se asigna (`None` si no hay)."""
    arbol = ast.parse(fuente)
    ambito = {}
    for top in arbol.body:
        nombre = None
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombre = top.name
        elif isinstance(top, (ast.Assign, ast.AnnAssign)):
            objetivos = top.targets if isinstance(top, ast.Assign) else [top.target]
            nombre = next((t.id for t in objetivos if isinstance(t, ast.Name)), None)
        for n in ast.walk(top):
            if hasattr(n, 'lineno'):
                ambito.setdefault((n.lineno, getattr(n, 'col_offset', 0)), nombre)
    out = []
    for n in ast.walk(arbol):
        que = None
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and n.value in NIVELES_LITERALES:
            que = repr(n.value)
        elif isinstance(n, ast.Name) and n.id in NOMBRES_DE_NIVEL \
                and isinstance(n.ctx, ast.Load):
            que = n.id
        elif isinstance(n, ast.Attribute) and n.attr in NOMBRES_DE_NIVEL:
            que = n.attr
        if que is not None:
            out.append((n.lineno, que, ambito.get((n.lineno, n.col_offset))))
    return out


def _archivos_py():
    for base in ('flota', 'app'):
        for p in sorted((RAIZ / base).rglob('*.py')):
            if '__pycache__' not in p.parts:
                yield p


class TestNadieMasDecideElNivelPython:

    def test_ningun_nivel_fuera_de_la_politica(self):
        malos = []
        for p in _archivos_py():
            if p == LA_POLITICA:
                continue
            rel = str(p.relative_to(RAIZ))
            for linea, que, ambito in _violaciones_con_ambito(p.read_text(encoding='utf-8')):
                if rel not in INVENTARIO_PY and (rel, ambito) not in OTROS_SEMAFOROS:
                    malos.append(f'{rel}:{linea} {que}')
        assert not malos, (
            '\nUn nivel de vehículo decidido fuera de flota/dominio/salida.py:\n  '
            + '\n  '.join(malos)
            + '\n\nUsá el `.nivel` de un Motivo de la política (o una constante '
              'con nombre DENTRO de salida.py, como NIVEL_TURNO_A_REVISAR).')

    def test_el_inventario_solo_encoge(self):
        assert len(INVENTARIO_PY) <= 0

    def test_ve_las_formas_que_importan(self):
        casos = ["x = 'rojo'", "if n == 'ambar': pass", "c = {'amarillo': 1}",
                 "y = ROJO", "z = salida.AMBAR", "f(nivel='verde')"]
        for c in casos:
            assert _violaciones_py(c), c

    def test_no_marca_lo_sano(self):
        sano = ('"""El semáforo pinta rojo cuando…"""\n'
                "# rojo o ámbar, decidido por la política\n"
                "from flota.dominio.salida import ROJO\n"
                "t = 'Rojo es no debería salir'\n"
                "n = motivo.nivel\n")
        assert _violaciones_py(sano) == []

    def test_otros_semaforos_no_crecen_y_dicen_por_que(self):
        assert len(OTROS_SEMAFOROS) <= 4
        for (archivo, ambito), motivo in OTROS_SEMAFOROS.items():
            assert len(motivo) > 20, (archivo, ambito)
            hits = [a for _l, _q, a in _violaciones_con_ambito(
                (RAIZ / archivo).read_text(encoding='utf-8'))]
            # Una exención que ya no exime nada se borra (solo encoge).
            assert ambito in hits, (archivo, ambito)

    def test_la_exencion_es_por_ambito_no_por_archivo(self):
        """Un nivel de vehículo en OTRA función de un archivo con un semáforo
        propio sigue siendo una violación."""
        src = ("def semaforo(v):\n    return 'rojo'\n"
               "def color_del_camion(v):\n    return 'rojo'\n"
               "NIVELES_SEMAFORO = ('verde',)\n")
        ambitos = [a for _l, _q, a in _violaciones_con_ambito(src)]
        assert sorted(ambitos) == ['NIVELES_SEMAFORO', 'color_del_camion', 'semaforo']
        malos = [a for a in ambitos
                 if ('app/services/analitica_kpi.py', a) not in OTROS_SEMAFOROS]
        assert malos == ['color_del_camion']

    def test_piso_el_escaner_ve_la_politica(self):
        """Si el escáner se rompe devuelve cero y el test de arriba pasa
        tranquilo. La política misma tiene que dar positivo."""
        hits = _violaciones_py(LA_POLITICA.read_text(encoding='utf-8'))
        assert len(hits) >= 10, hits
        assert sum(1 for _ in _archivos_py()) >= 200


# ── El teléfono y la bandeja no deciden niveles ─────────────────────────────

FLOTA_JS = sorted((RAIZ / 'app' / 'static' / 'pwa').glob('flota*.js'))

#: Un nivel que NACE en el JS: el primer elemento de una tupla (`['rojo', …]`,
#: la forma vieja de `flotaCondAvisos`), el resultado de un ternario, o una
#: asignación a `nivel`/`color`/`urgencia`.
_NACE_EN_JS = re.compile(
    r"\[\s*'(?:rojo|ambar|amarillo|verde)'\s*,"
    r"|[?:]\s*'(?:rojo|ambar|amarillo|verde)'\s*[;:),]"
    r"|\b(?:nivel|color|urgencia)\s*[:=]\s*'(?:rojo|ambar|amarillo|verde)'")


def _sin_comentarios(fuente):
    from tests.flota.test_trinquetes_flota import _sin_comentarios as f
    return f(fuente)


def _js_violaciones(fuente):
    codigo = _sin_comentarios(fuente)
    out = [m.group(0) for m in _NACE_EN_JS.finditer(codigo)]
    # Un vocabulario: «ámbar», no «amarillo», en todo el código de flota.
    out += re.findall(r"amarillo", codigo)
    return out


class TestNadieMasDecideElNivelJS:

    def test_ningun_nivel_nace_en_el_telefono_ni_en_la_bandeja(self):
        malos = {p.name: _js_violaciones(p.read_text(encoding='utf-8'))
                 for p in FLOTA_JS}
        malos = {k: v for k, v in malos.items() if v}
        assert not malos, malos

    def test_ve_la_forma_vieja(self):
        viejo = ("lineas.push(['rojo', `x`, 'y']);\n"
                 "const nivel = a ? 'rojo' : 'amarillo';\n"
                 "p.urgencia = 'ambar';")
        assert len(_js_violaciones(viejo)) >= 3

    def test_no_marca_lo_que_pinta_lo_que_manda_el_servidor(self):
        sano = ("// rojo o amarillo, lo decide el servidor\n"
                "const FLOTA_SEMAFORO = { rojo: {etiqueta: 'Atender hoy'} };\n"
                "return urgencia === 'rojo' ? chipHoy : chipPlazo;\n"
                "const c = color[n];\n")
        assert _js_violaciones(sano) == []

    def test_piso(self):
        assert len(FLOTA_JS) >= 4
        assert any('flotaCondAvisos' in p.read_text(encoding='utf-8') for p in FLOTA_JS)
