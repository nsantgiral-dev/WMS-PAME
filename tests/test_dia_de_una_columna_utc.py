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

## Las otras tres escrituras del mismo error (2026-09-24)

`.date()` en Python no era la única forma de sacar el día UTC. El trinquete no
veía estas, y las tres estaban en `app/`:

| Forma | Sitio | Qué rompía |
|---|---|---|
| `cast(col, Date)` en SQL | `dashboard_service._tendencia_7d` | Lo hecho entre las 7 p. m. y la medianoche se contaba en la barra de mañana |
| `func.date(col) == hoy` | `muelle_service.obtener_manifiesto` | Un bulto cargado a las 8 p. m. salía del manifiesto de hoy |
| `x = r.fecha_x` … `x.date()` | `rezago_liquidacion.fecha_de_referencia` | Una ruta entregada el 31 a las 9 p. m. no «cruzaba mes» (y una del 30, sí, al revés) |

La tercera es la misma llamada a través de un nombre: el detector miraba el
atributo y no lo que se le asignó. Ahora sigue la asignación dentro de la
función (`x = obj.fecha_…` o `x = getattr(obj, 'fecha_…')`).

Se escanean `app/` y `flota/` (el paquete de flota vive fuera de `app/`).

**Lo que NO ve** (probado por mutación el 2026-09-24): el `.date()` sobre la
variable de un `for` que recorre una consulta —`for (m,) in query(T.fecha_x):
m.date()`—. El detector sigue asignaciones, no iteraciones; ese caso lo cubre
el test de borde de noche de cada sitio, no el trinquete.
"""
import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PREFIJOS = ('fecha', 'created', 'updated')

#: Sitios que pueden tomar el día UTC de una columna, con su porqué. Vacío.
EXCEPCIONES: dict = {}


def _es_columna_fecha(n) -> bool:
    return isinstance(n, ast.Attribute) and n.attr.startswith(PREFIJOS)


def _nombre_de(n) -> str:
    return n.attr if isinstance(n, ast.Attribute) else getattr(n, 'id', '?')


_AMBITOS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)


def _nodos_del_ambito(ambito):
    """Los nodos de una función (o del módulo) SIN entrar a funciones anidadas:
    un alias vale en su ámbito y en ningún otro."""
    pila = list(ast.iter_child_nodes(ambito))
    while pila:
        n = pila.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            pila.extend(ast.iter_child_nodes(n))


def _alias_de_fecha(arbol):
    """{(id(función), nombre)} de variables asignadas desde una columna de fecha:
    `x = r.fecha_x` o `x = getattr(r, 'fecha_x', ...)`."""
    out = {}
    for f in ast.walk(arbol):
        if not isinstance(f, _AMBITOS):
            continue
        for n in _nodos_del_ambito(f):
            if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)):
                continue
            v = n.value
            origen = None
            if _es_columna_fecha(v):
                origen = v.attr
            elif (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                  and v.func.id == 'getattr' and len(v.args) >= 2
                  and isinstance(v.args[1], ast.Constant)
                  and isinstance(v.args[1].value, str)
                  and v.args[1].value.startswith(PREFIJOS)):
                origen = v.args[1].value
            if origen:
                out[(id(f), n.targets[0].id)] = origen
    return out


def _sitios(fuente: str, nombre: str = '<src>'):
    """[(archivo, línea, columna)] — día UTC sacado de una columna, en sus
    cuatro escrituras: `col.date()`, `x.date()` con `x = col`,
    `cast(col, Date)` y `func.date(col)`."""
    arbol = ast.parse(fuente)
    alias = _alias_de_fecha(arbol)
    out = []
    # `.date()` sobre el atributo o sobre un alias de la misma función
    funciones = [f for f in ast.walk(arbol) if isinstance(f, _AMBITOS)]
    vistos = set()
    for f in funciones:
        for n in _nodos_del_ambito(f):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == 'date' and not n.args and not n.keywords):
                continue
            base = n.func.value
            col = None
            if _es_columna_fecha(base):
                col = base.attr
            elif isinstance(base, ast.Name) and (id(f), base.id) in alias:
                col = alias[(id(f), base.id)]
            if col and (n.lineno, n.col_offset) not in vistos:
                vistos.add((n.lineno, n.col_offset))
                out.append((nombre, n.lineno, col))
    # SQL: `cast(col, Date)` y `func.date(col)`
    for n in ast.walk(arbol):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        f = n.func
        # `cast`, `sa.cast`, `sa_cast`…: cualquier nombre que termine en cast.
        es_cast = (_nombre_de(f).endswith('cast') and len(n.args) == 2
                   and _nombre_de(n.args[1]) == 'Date')
        # `func.date`, `db.func.date`, `sa_func.date`.
        es_func_date = (isinstance(f, ast.Attribute) and f.attr == 'date'
                        and _nombre_de(f.value).endswith('func'))
        if (es_cast or es_func_date) and _es_columna_fecha(n.args[0]):
            out.append((nombre, n.lineno, n.args[0].attr))
    return out


def _archivos():
    return sorted([*(RAIZ / 'app').rglob('*.py'), *(RAIZ / 'flota').rglob('*.py')])


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
               'w = s.fecha_x.date(tz)\n'
               'q = cast(T.cantidad, Integer)\n'
               'r = func.date(ahora)\n'
               'def f(r):\n'
               '    x = r.fecha_x\n'
               '    return dia_operativo_de(x)\n'
               'def g(r):\n'
               '    x = ahora_bogota()\n'
               '    return x.date()\n')
        assert _sitios(src) == []

    def test_ve_el_cast_y_el_func_date(self):
        src = ('a = db.session.query(cast(T.fecha_completado, Date))\n'
               'b = Q.filter(func.date(B.fecha_cargado) == hoy)\n'
               'c = Q.filter(db.func.date(B.created_at) == hoy)\n'
               'd = sa.cast(X.updated_at, sa.Date)\n'
               'e = sa_cast(X.fecha_y, Date)\n'
               'g = sa_func.date(X.fecha_z)\n')
        assert sorted(a for _, _, a in _sitios(src)) == sorted([
            'fecha_completado', 'fecha_cargado', 'created_at', 'updated_at',
            'fecha_y', 'fecha_z'])

    def test_ve_el_alias_dentro_de_la_funcion(self):
        """La forma de `rezago_liquidacion`: la columna pasa por un nombre."""
        src = ('def f(ruta):\n'
               "    entregada = getattr(ruta, 'fecha_entregada', None)\n"
               '    return entregada.date()\n'
               'def g(r):\n'
               '    x = r.created_at\n'
               '    return x.date()\n')
        assert [a for _, _, a in _sitios(src)] == ['fecha_entregada', 'created_at']

    def test_piso_de_formas(self):
        """Las tres escrituras nuevas existían en `app/` el 2026-09-24. Si el
        escáner deja de ver una de ellas sobre el repo, el cero no dice nada:
        se mide contra el fuente ANTERIOR, fijado acá."""
        viejo = (
            'def _tendencia_7d():\n'
            '    rows = db.session.query(cast(TareaPicking.fecha_completado, Date)).group_by(\n'
            '        cast(TareaPicking.fecha_completado, Date)).all()\n'
            'def obtener_manifiesto():\n'
            '    return Bulto.query.filter(func.date(Bulto.fecha_cargado) == hoy)\n'
            'def fecha_de_referencia(ruta):\n'
            "    entregada = getattr(ruta, 'fecha_entregada', None)\n"
            '    if entregada is not None:\n'
            "        return entregada.date() if hasattr(entregada, 'date') else entregada\n")
        assert len(_sitios(viejo)) == 4

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


class TestLasTresFormasNuevasCuentanElDiaDeBogota:
    """El borde de la noche para cada sitio arreglado el 2026-09-24: 22:35 en
    Bogotá del 23 = 03:35 UTC del 24. Lo hecho ahí es del 23."""

    AHORA_UTC = __import__('datetime').datetime(2026, 9, 24, 3, 35)

    def _reloj(self, monkeypatch):
        from datetime import timedelta
        monkeypatch.setattr('app.utils.fecha.ahora_bogota',
                            lambda: self.AHORA_UTC - timedelta(hours=5))

    def test_la_tendencia_pone_lo_de_la_noche_en_la_barra_de_hoy(
            self, db, monkeypatch, almacen, producto, ub_picking):
        from app.models.picking import TareaPicking
        from app.services.dashboard_service import _tendencia_7d
        self._reloj(monkeypatch)
        db.session.add(TareaPicking(
            codigo='TP-NOCHE', producto_id=producto.id, cantidad_solicitada=1,
            ubicacion_id=ub_picking.id, almacen_id=almacen.id,
            estado='COMPLETADO', fecha_completado=self.AHORA_UTC))
        db.session.commit()
        dias = _tendencia_7d()
        assert dias[-1]['fecha'] == '23/09'
        assert dias[-1]['picking'] == 1, dias

    def test_el_manifiesto_de_hoy_incluye_el_bulto_de_las_ocho(
            self, db, monkeypatch, almacen):
        from datetime import datetime
        from app.models.bulto import Bulto, EstadoBulto
        from app.models.packing import TareaPacking
        from app.services.muelle_service import MuelleService
        self._reloj(monkeypatch)
        t = TareaPacking(codigo='PACK-NOCHE', numero_pedido_siesa='PD-NOCHE',
                         almacen_id=almacen.id, estado='COMPLETADO', cliente='C')
        db.session.add(t)
        db.session.flush()
        db.session.add(Bulto(tarea_id=t.id, codigo_barras='PD-NOCHE-01', tipo='Caja',
                             numero=1, total=1, estado=EstadoBulto.CARGADO,
                             fecha_cargado=datetime(2026, 9, 24, 1, 0)))  # 8 p. m. del 23
        db.session.commit()
        m = MuelleService.obtener_manifiesto()
        assert m['fecha'] == '2026-09-23'
        assert m['total_bultos'] == 1

    def test_la_ruta_entregada_el_31_de_noche_cruza_mes(self):
        from datetime import date, datetime
        from app.services import rezago_liquidacion as rz

        class _Ruta:
            fecha_entregada = datetime(2026, 8, 1, 2, 0)   # 31-jul 9 p. m. Bogotá
            fecha_programada = None
        assert rz.fecha_de_referencia(_Ruta()) == date(2026, 7, 31)
        assert rz.urgencia(_Ruta(), date(2026, 8, 1)) == rz.CRUZA_MES
