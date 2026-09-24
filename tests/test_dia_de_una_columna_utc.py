"""Un «hace cuántos días» o un día que alguien LEE no sale de `.date()` sobre
una columna UTC (Regla 5).

## El defecto (2026-09-23, 22:35 Bogotá)

Cinco sitios hacían `hoy_bogota - s.fecha_x.date()`: el lado izquierdo es el
día operativo y el derecho el día **UTC** de la columna. Entre las 7 p. m. y
la medianoche de Bogotá el día UTC ya es mañana, y la resta da uno menos. Lo
destapó la suite local corrida a esa hora: `TRA-30` dejaba de reportar un
traslado justo en el umbral y `TRA-31` contaba un día de menos. En Railway
(UTC) rompe el build cinco horas al día.

La forma correcta ya existía: `app.utils.fecha.dia_operativo_de`.

## El trinquete

Por AST: ninguna llamada `.date()` sin argumentos sobre un atributo cuyo
nombre empieza por `fecha`, `created` o `updated` en `app/`. Las columnas de
fecha del WMS son `DateTime` UTC; su `.date()` es siempre el día UTC. El
inventario de excepciones está vacío y solo puede encoger.
"""
import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PREFIJOS = ('fecha', 'created', 'updated')

#: Sitios que pueden tomar el día UTC de una columna, con su porqué. Vacío.
EXCEPCIONES: dict = {}


def _sitios(fuente: str, nombre: str = '<src>'):
    out = []
    for n in ast.walk(ast.parse(fuente)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'date' and not n.args and not n.keywords
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr.startswith(PREFIJOS)):
            out.append((nombre, n.lineno, n.func.value.attr))
    return out


def _archivos():
    return sorted((RAIZ / 'app').rglob('*.py'))


def test_ningun_dia_sale_de_date_sobre_una_columna_utc():
    encontrados = []
    for p in _archivos():
        rel = str(p.relative_to(RAIZ))
        for sitio in _sitios(p.read_text(encoding='utf-8'), rel):
            if (sitio[0], sitio[2]) not in EXCEPCIONES:
                encontrados.append(sitio)
    assert not encontrados, (
        'Día UTC de una columna usado como día de Bogotá: usá '
        f'app.utils.fecha.dia_operativo_de. Encontrado: {encontrados}')


def test_las_excepciones_dicen_por_que():
    assert all(isinstance(v, str) and len(v) > 20 for v in EXCEPCIONES.values())


class TestElDetectorMuerde:

    def test_ve_la_forma_rota(self):
        src = 'dias = (hoy - s.fecha_despacho.date()).days\nx = r.created_at.date()\n'
        assert {a for _, _, a in _sitios(src)} == {'fecha_despacho', 'created_at'}

    def test_no_marca_lo_sano(self):
        src = ('hoy = ahora_bogota().date()\n'
               'd = dia_operativo_de(s.fecha_despacho)\n'
               'z = datetime.date(2026, 1, 1)\n'
               'w = s.fecha_x.date(tz)\n')
        assert _sitios(src) == []

    def test_piso_de_archivos(self):
        """Si el escaneo deja de encontrar archivos, un cero no dice nada."""
        assert len(_archivos()) >= 100


def test_el_borde_de_la_noche_cuenta_los_dias_de_bogota(db, usuario, monkeypatch):
    """El caso que rompió: 22:35 en Bogotá (03:35 UTC del día siguiente), un
    traslado despachado exactamente `DIAS_EN_TRANSITO_ANORMAL` días antes."""
    from datetime import datetime, timedelta
    from app.models.traslado import EstadoTraslado, SolicitudTraslado
    from app.services.auditoria import traslados as tra
    ahora_utc = datetime(2026, 9, 24, 3, 35)
    monkeypatch.setattr('app.utils.fecha.ahora_bogota',
                        lambda: ahora_utc - timedelta(hours=5))
    db.session.add(SolicitudTraslado(
        codigo='ST-NOCHE', bodega_origen_siesa='NB1', bodega_destino_siesa='NS1',
        estado=EstadoTraslado.EN_TRANSITO, solicitante_id=usuario.id,
        fecha_despacho=ahora_utc - timedelta(days=tra.DIAS_EN_TRANSITO_ANORMAL)))
    db.session.commit()
    h = [x for x in tra.se_pueden_contar_los_traslados_en_vuelo() if x.referencia == 'ST-NOCHE']
    assert h and h[0].datos['dias'] == tra.DIAS_EN_TRANSITO_ANORMAL
