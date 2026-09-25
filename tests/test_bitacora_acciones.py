"""
Bitácora de acciones (Fase 0 de analítica, 2026-09-24).

La clase del defecto: **se borra, cancela, anula o reescribe algo del
recorrido pedido → caja sin dejar quién, cuándo ni por qué.** No era un
sitio: eran veinte. `cancelar_picking` recibía el motivo y lo tiraba,
`reabrir_picking` borraba al operario, reintentar un job borraba el error,
`forzar_cierre_ruta` pisaba la hora de salida, la liquidación no tenía fecha
ni autor, y retractar un juicio de temporada lo borraba de una tabla
analítica protegida.

Tres partes, en el orden de «Cómo se cierra un defecto acá»:

1. **Trinquetes con inventario declarado** (por AST, sobre `app/` y
   `flota/`): todo borrado físico y toda asignación de un estado
   CANCELADO/ANULADO/DESCARTADO/REVERTIDO vive en una función que llama a
   `registrar_accion` — o en un inventario de excepciones que dice por qué y
   que solo encoge. Más tres de «una política, una función»: solo
   `reencolar_job_fallido` borra el error de un job, solo
   `PickingService._soltar_operario` suelta al operario de una tarea, solo
   `RutaService._marcar_liquidada` escribe LIQUIDADA.
2. **Meta-tests**: el detector ve la forma rota (las escrituras distintas de
   la misma operación), no marca lo sano, y tiene piso — un escáner que se
   desincroniza devuelve cero, y cero se lee igual que «no hay nada».
3. **Comportamiento, por la puerta HTTP real**: cada acción deja su fila con
   usuario, motivo y antes/después, y el motivo vacío se rechaza donde es
   obligatorio.
"""
import ast
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════════════════
# 1. El escáner
# ═════════════════════════════════════════════════════════════════════════

def _funciones(arbol):
    """`[(qualname, nodo)]`, incluido el módulo como `<modulo>`.

    Cada función se mide por **sus propios** nodos (sin las funciones
    anidadas): una llamada a `registrar_accion` en una función hija no
    registra lo que borra la madre.
    """
    out = [('<modulo>', arbol)]

    def visitar(nodo, prefijo):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f'{prefijo}{hijo.name}'
                out.append((q, hijo))
                visitar(hijo, q + '.')
            elif isinstance(hijo, ast.ClassDef):
                visitar(hijo, f'{prefijo}{hijo.name}.')
            else:
                visitar(hijo, prefijo)

    visitar(arbol, '')
    return out


_ANIDADAS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _propios(fn):
    pila = [n for n in fn.body if not isinstance(n, _ANIDADAS)]
    while pila:
        n = pila.pop()
        yield n
        for hijo in ast.iter_child_nodes(n):
            if not isinstance(hijo, _ANIDADAS):
                pila.append(hijo)


_DELETE_SQL = re.compile(r'^\s*DELETE\s+FROM\b', re.IGNORECASE)
_ESTADOS_DE_BAJA = ('CANCELAD', 'ANULAD', 'DESCARTAD', 'REVERTID')


def _es_borrado(n) -> bool:
    """Las tres escrituras de «borrar una fila».

    `db.session.delete(x)` y `Query.delete()` son la misma operación con dos
    sintaxis (las dos son `.delete(`); `delete(Tabla)` de SQLAlchemy y un
    `text('DELETE FROM …')` son las otras dos. Un regex sobre el texto ve una
    y se atrapa en los docstrings: el árbol no.
    """
    if not isinstance(n, ast.Call):
        return False
    f = n.func
    if isinstance(f, ast.Attribute) and f.attr == 'delete':
        return True
    if isinstance(f, ast.Name) and f.id == 'delete':
        return True
    for arg in n.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                and _DELETE_SQL.match(arg.value):
            return True
    return False


def _pone_estado_de_baja(n) -> bool:
    """`x.estado = …CANCELADO` y `{'estado': 'CANCELADA'}` (el `.update()`)."""
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Attribute) and t.attr == 'estado':
                v = ast.unparse(n.value).upper()
                if any(e in v for e in _ESTADOS_DE_BAJA):
                    return True
    if isinstance(n, ast.Dict):
        for k, v in zip(n.keys, n.values):
            if isinstance(k, ast.Constant) and k.value == 'estado' and v is not None \
                    and any(e in ast.unparse(v).upper() for e in _ESTADOS_DE_BAJA):
                return True
    return False


def _asigna_none(n, atributo) -> bool:
    return (isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Constant) and n.value.value is None
            and any(isinstance(t, ast.Attribute) and t.attr == atributo for t in n.targets))


def _escribe_liquidada(n) -> bool:
    return (isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == 'estado_financiero'
                    for t in n.targets)
            and 'LIQUIDADA' in ast.unparse(n.value).upper())


def _registra(nodos) -> bool:
    for n in nodos:
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == 'registrar_accion') or \
                    (isinstance(f, ast.Attribute) and f.attr == 'registrar_accion'):
                return True
    return False


def escanear(fuente: str, predicado):
    """`{qualname: (lineas, registra)}` de las funciones donde `predicado` pega."""
    out = {}
    for q, fn in _funciones(ast.parse(fuente)):
        nodos = list(_propios(fn))
        lineas = sorted(getattr(n, 'lineno', 0) for n in nodos if predicado(n))
        if lineas:
            out[q] = (lineas, _registra(nodos))
    return out


def _repo(predicado):
    """`{(archivo, qualname): (lineas, registra)}` sobre `app/` y `flota/`."""
    out = {}
    for base in ('app', 'flota'):
        for p in sorted((RAIZ / base).rglob('*.py')):
            rel = p.relative_to(RAIZ).as_posix()
            for q, v in escanear(p.read_text(encoding='utf-8'), predicado).items():
                out[(rel, q)] = v
    return out


# ═════════════════════════════════════════════════════════════════════════
# 2. Los inventarios — cada entrada dice por qué. Solo encogen.
# ═════════════════════════════════════════════════════════════════════════

#: Funciones que borran filas **y** registran lo borrado en la misma función.
BORRADOS_REGISTRADOS = {
    ('app/routes/config_siesa.py', 'eliminar_mapeo'):
        'Configuración: un mapeo borrado deja productos sin unidad de negocio; '
        'la fila entera queda en el antes.',
    ('app/routes/picking.py', 'purgar_picks_cero'):
        'Limpieza técnica de picks en cero: exige motivo y guarda cada fila.',
    ('app/services/layout_service.py', '_borrar_historial_de'):
        'Eliminación FORZADA de ubicación: borra tareas y sesiones de conteo '
        'reales; cada una deja su foto y las desvinculadas quedan listadas.',
    ('app/services/layout_service.py', '_borrar_ubicacion'):
        'Única vía que borra un hueco (ubicación, fila, cuerpo, remodular): '
        'guarda el hueco y sus asignaciones.',
    ('app/services/packing_service.py', 'PackingService.cancelar'):
        'Cancelar empaque borra los bultos sin cargar: cada uno queda escrito.',
    ('app/services/packing_service.py', 'PackingService.resetear_siesa'):
        'Reset tras fallo de Siesa: borra los bultos para redeclararlos y '
        'guarda el siesa_response que borra.',
    ('app/services/picking_service.py', 'PickingService.auditar_tarea'):
        'La auditoría saca del empaque la línea que ya no tiene qué empacar.',
    ('app/services/ruta_service.py', 'RutaService.actualizar_maestra'):
        'Editar paradas las reescribe enteras: las viejas quedan en el antes.',
    ('app/services/ruta_service.py', 'RutaService.eliminar_maestra'):
        'Solo se borra una maestra sin viajes: queda la foto con sus paradas.',
}

#: Funciones que borran **sin** bitácora. Cada una con su porqué.
BORRADOS_SIN_BITACORA = {
    ('app/services/pedidos_sync_service.py', '_run_sync'):
        'Espejo de Siesa: borra las líneas de pedidos que Siesa dejó de '
        'reportar (solo con barrido completo, ver K del 2026-08-13). No es una '
        'decisión de una persona sino el reflejo del ERP, `pedidos_siesa` es '
        'REGENERABLE en la verificación de respaldo, y el archivo lo trabaja la '
        'Fase 0B (instrucción de no tocarlo en la 0A).',
}
TOPE_BORRADOS_SIN_BITACORA = 1

#: Funciones que ponen un estado de baja **sin** registrar_accion.
ESTADOS_SIN_BITACORA = {
    ('app/routes/conteo.py', 'omitir_segundo_conteo'):
        'Conteo, fuera de la Fase 0A por instrucción. Rastro que SÍ tiene: '
        'fecha_cierre en cada eslabón y la raíz en DESCUADRE. Rastro que NO: '
        'quién omitió (solo el log) — declarado en CLAUDE.md.',
    ('app/routes/conteo.py', 'descartar_fallos_dlq'):
        'Conteo, fuera de la Fase 0A por instrucción. El job descartado guarda '
        'por_usuario y fecha en `resultado`; sin motivo.',
    ('app/services/abc_service.py', 'ABCService.cancelar_rezago'):
        'Conteo: «Limpiar cola cancela, no borra» — exige motivo y lo escribe '
        'en motivo_edicion con editado_por/editado_en en cada sesión.',
    ('app/services/conteo_service.py', 'ConteoService.cancelar_cadena'):
        'Conteo: cancelar_cadena exige motivo y lo deja en la sesión con quién '
        '(ver «Conteo: ninguna cadena queda sin salida»).',
    ('flota/adaptadores/hallazgos.py', 'descartar'):
        'Flota tiene rastro propio: cerrado_por_usuario_id, cerrado_ts y '
        'motivo_cierre obligatorio, respaldado por un CHECK.',
    ('flota/adaptadores/taller.py', 'anular'):
        'Flota tiene rastro propio: cerrada_por_usuario_id, cerrada_ts y '
        'motivo_cierre obligatorio, respaldado por un CHECK.',
}
TOPE_ESTADOS_SIN_BITACORA = 6

#: Quién puede borrar el error de un job. Una sola función.
LIMPIAN_ERROR_DE_JOB = {('app/services/siesa_job_service.py', 'reencolar_job_fallido')}
LIMPIAN_ERROR_SIN_BITACORA = {
    ('app/routes/conteo.py', 'reintentar_fallos_dlq'):
        'Conteo, fuera de la Fase 0A por instrucción: re-encola los AJUSTE_CONTEO '
        'FALLIDO borrando error_ultimo sin dejarlo escrito. Pasarlo a '
        'reencolar_job_fallido es una línea; queda declarado para el dueño de conteo.',
}

#: Quién puede soltar al operario de una tarea.
SUELTAN_OPERARIO = {('app/services/picking_service.py', 'PickingService._soltar_operario')}
SUELTAN_OPERARIO_OTRA_ENTIDAD = {
    ('app/services/conteo_service.py', 'ConteoService.devolver_al_pool'):
        'Es SesionConteo, no TareaPicking: devolver_al_pool deja lo parcial en '
        'conteos_descartados con su motivo antes de soltar.',
}

ESCRIBEN_LIQUIDADA = {('app/services/ruta_service.py', 'RutaService._marcar_liquidada')}


# ═════════════════════════════════════════════════════════════════════════
# 3. Trinquetes
# ═════════════════════════════════════════════════════════════════════════

class TestNingunBorradoSinRastro:

    def test_todo_borrado_esta_declarado(self):
        sitios = _repo(_es_borrado)
        declarados = set(BORRADOS_REGISTRADOS) | set(BORRADOS_SIN_BITACORA)
        nuevos = {k: v[0] for k, v in sitios.items() if k not in declarados}
        assert not nuevos, (
            f'Borrado físico nuevo sin declarar: {nuevos}. Registralo con '
            'registrar_accion (antes=foto(fila)) y agregalo a '
            'BORRADOS_REGISTRADOS, o convertilo en anulación.')

    def test_cada_borrado_registrado_llama_a_registrar_accion(self):
        sitios = _repo(_es_borrado)
        sin = [k for k in BORRADOS_REGISTRADOS if k in sitios and not sitios[k][1]]
        assert not sin, f'Borran sin registrar_accion en la misma función: {sin}'

    def test_el_inventario_solo_encoge(self):
        sitios = _repo(_es_borrado)
        viejos = [k for k in list(BORRADOS_REGISTRADOS) + list(BORRADOS_SIN_BITACORA)
                  if k not in sitios]
        assert not viejos, f'Ya no borran: sacalos del inventario {viejos}'
        assert len(BORRADOS_SIN_BITACORA) <= TOPE_BORRADOS_SIN_BITACORA

    def test_cada_excepcion_dice_por_que(self):
        for inv in (BORRADOS_REGISTRADOS, BORRADOS_SIN_BITACORA,
                    ESTADOS_SIN_BITACORA, LIMPIAN_ERROR_SIN_BITACORA,
                    SUELTAN_OPERARIO_OTRA_ENTIDAD):
            for k, razon in inv.items():
                assert len(razon.strip()) >= 40, f'{k}: la razón no explica nada'


class TestNingunaBajaSinRastro:

    def test_toda_baja_de_estado_registra(self):
        sitios = _repo(_pone_estado_de_baja)
        rotos = {k: v[0] for k, v in sitios.items()
                 if not v[1] and k not in ESTADOS_SIN_BITACORA}
        assert not rotos, (
            f'Ponen CANCELADO/ANULADO/DESCARTADO/REVERTIDO sin registrar_accion: '
            f'{rotos}')

    def test_las_excepciones_solo_encogen(self):
        sitios = _repo(_pone_estado_de_baja)
        viejas = [k for k in ESTADOS_SIN_BITACORA if k not in sitios or sitios[k][1]]
        assert not viejas, f'Ya no aplica la excepción (sacala): {viejas}'
        assert len(ESTADOS_SIN_BITACORA) <= TOPE_ESTADOS_SIN_BITACORA


class TestUnaPoliticaUnaFuncion:

    def test_solo_una_funcion_borra_el_error_de_un_job(self):
        sitios = set(_repo(lambda n: _asigna_none(n, 'error_ultimo')))
        assert sitios - LIMPIAN_ERROR_DE_JOB - set(LIMPIAN_ERROR_SIN_BITACORA) == set()
        assert LIMPIAN_ERROR_DE_JOB <= sitios

    def test_solo_una_funcion_suelta_al_operario(self):
        sitios = set(_repo(lambda n: _asigna_none(n, 'operario_id')))
        assert sitios - SUELTAN_OPERARIO - set(SUELTAN_OPERARIO_OTRA_ENTIDAD) == set()
        assert SUELTAN_OPERARIO <= sitios

    def test_solo_una_funcion_escribe_liquidada(self):
        sitios = set(_repo(_escribe_liquidada))
        assert sitios == ESCRIBEN_LIQUIDADA


# ═════════════════════════════════════════════════════════════════════════
# 4. Meta-tests: el escáner muerde, no marca lo sano, y tiene piso
# ═════════════════════════════════════════════════════════════════════════

_ROTO = '''
def a(x):
    db.session.delete(x)

def b(tid):
    Bulto.query.filter_by(tarea_id=tid).delete()

def c():
    db.session.execute(delete(Bulto))

def d():
    db.session.execute(text("DELETE FROM bultos WHERE id = 1"))

def e(t):
    t.estado = EstadoPicking.CANCELADO

def f(q):
    q.update({'estado': 'ANULADA'})

def g(s):
    s.estado = 'REVERTIDA'
'''

_SANO = '''
def a(x):
    """Acá se habla de db.session.delete(x) y de DELETE FROM bultos."""
    # db.session.delete(x)
    registrar_accion('ELIMINAR', x)
    db.session.delete(x)

def b(d):
    d.pop('k')
    lista.remove(3)

def c(t):
    t.estado = 'COMPLETADO'
    registrar_accion('CANCELAR', t)
    t.estado = EstadoPicking.CANCELADO
'''


class TestElEscanerMuerde:

    def test_ve_las_cuatro_escrituras_del_borrado(self):
        sitios = escanear(_ROTO, _es_borrado)
        assert set(sitios) == {'a', 'b', 'c', 'd'}
        assert not any(reg for _, reg in sitios.values())

    def test_ve_las_tres_formas_de_la_baja(self):
        assert set(escanear(_ROTO, _pone_estado_de_baja)) == {'e', 'f', 'g'}

    def test_no_marca_lo_sano(self):
        borrados = escanear(_SANO, _es_borrado)
        assert set(borrados) == {'a'} and borrados['a'][1] is True
        bajas = escanear(_SANO, _pone_estado_de_baja)
        assert set(bajas) == {'c'} and bajas['c'][1] is True

    def test_registrar_en_una_funcion_hija_no_cuenta(self):
        fuente = ('def madre(x):\n'
                  '    def hija():\n'
                  '        registrar_accion("ELIMINAR", x)\n'
                  '    db.session.delete(x)\n')
        assert escanear(fuente, _es_borrado)['madre'][1] is False

    def test_piso(self):
        """Si el escáner se desincroniza devuelve cero, y cero parece «nada que hacer»."""
        borrados = _repo(_es_borrado)
        assert len(borrados) >= 9
        assert sum(len(v[0]) for v in borrados.values()) >= 14
        assert len(_repo(_pone_estado_de_baja)) >= 15


# ═════════════════════════════════════════════════════════════════════════
# 5. La función misma
# ═════════════════════════════════════════════════════════════════════════

def _filas(**filtro):
    from app.models.bitacora import BitacoraAccion
    q = BitacoraAccion.query
    for k, v in filtro.items():
        q = q.filter(getattr(BitacoraAccion, k) == v)
    return q.order_by(BitacoraAccion.id).all()


class TestRegistrarAccion:

    def test_no_hace_commit_y_va_con_la_accion(self, app, db):
        from app.services.bitacora import registrar_accion
        registrar_accion('EDITAR', 'Cosa', 1, usuario_id=None, motivo='x')
        db.session.rollback()
        assert _filas(entidad='Cosa') == []

    def test_vocabulario_cerrado(self, app, db):
        from app.services.bitacora import registrar_accion
        with pytest.raises(ValueError, match='desconocida'):
            registrar_accion('BORRAR', 'Cosa', 1)

    def test_dia_operativo_es_bogota(self, app, db, monkeypatch):
        """A las 20:00 de Bogotá, en UTC ya es mañana (Regla 5)."""
        from datetime import datetime, date
        import app.services.bitacora as bit

        class _Reloj(datetime):
            @classmethod
            def utcnow(cls):
                return datetime(2026, 9, 25, 1, 0, 0)   # 20:00 del 24 en Bogotá
        monkeypatch.setattr(bit, 'datetime', _Reloj)
        fila = bit.registrar_accion('EDITAR', 'Cosa', 1)
        assert fila.dia_operativo == date(2026, 9, 24)

    def test_toma_id_codigo_y_almacen_de_la_fila(self, app, db, almacen, producto):
        from app.models.picking import TareaPicking
        from app.models.ubicacion import Ubicacion
        from app.services.bitacora import registrar_accion
        ub = Ubicacion(codigo='BIT-UB', almacen_id=almacen.id, tipo_zona='PICKING', activo=True)
        db.session.add(ub)
        db.session.flush()
        t = TareaPicking(codigo='PK-BIT-1', producto_id=producto.id, cantidad_solicitada=1,
                         ubicacion_id=ub.id, almacen_id=almacen.id)
        db.session.add(t)
        db.session.flush()
        f = registrar_accion('CANCELAR', t, antes={'monto': __import__('decimal').Decimal('1.50')})
        assert (f.entidad, f.entidad_id, f.entidad_codigo, f.almacen_id) == \
            ('TareaPicking', t.id, 'PK-BIT-1', almacen.id)
        assert f.antes == {'monto': '1.50'}

    def test_motivo_vacio(self):
        from app.services.bitacora import motivo_obligatorio, MotivoRequerido
        for vacio in (None, '', '   '):
            with pytest.raises(MotivoRequerido):
                motivo_obligatorio(vacio)
        assert motivo_obligatorio('  ok ') == 'ok'


# ═════════════════════════════════════════════════════════════════════════
# 6. Comportamiento por la puerta HTTP
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def h(jwt_token_admin):
    return {'Authorization': f'Bearer {jwt_token_admin}'}


@pytest.fixture
def ub(db, almacen):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo='BIT-PIK-1', almacen_id=almacen.id, tipo_zona='PICKING',
                  activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _tarea_picking(db, almacen, producto, ub, **kw):
    import uuid
    from app.models.picking import TareaPicking
    base = dict(codigo=f'PK-{uuid.uuid4().hex[:6]}', producto_id=producto.id,
                cantidad_solicitada=5, ubicacion_id=ub.id, almacen_id=almacen.id,
                estado='PENDIENTE')
    base.update(kw)
    t = TareaPicking(**base)
    db.session.add(t)
    db.session.commit()
    return t


class TestPicking:

    def test_cancelar_sin_motivo_se_rechaza(self, client, db, h, almacen, producto, ub):
        t = _tarea_picking(db, almacen, producto, ub)
        r = client.put(f'/api/picking/{t.id}/cancelar', json={}, headers=h)
        assert r.status_code == 400
        assert db.session.get(type(t), t.id).estado == 'PENDIENTE'
        assert _filas(entidad='TareaPicking') == []

    def test_cancelar_deja_quien_y_por_que(self, client, db, h, usuario_admin,
                                           usuario, almacen, producto, ub):
        t = _tarea_picking(db, almacen, producto, ub, operario_id=usuario.id,
                           estado='EN_PROCESO')
        r = client.put(f'/api/picking/{t.id}/cancelar', json={'motivo': 'ola mal lanzada'},
                       headers=h)
        assert r.status_code == 200
        [f] = _filas(accion='CANCELAR', entidad='TareaPicking')
        assert (f.usuario_id, f.motivo, f.entidad_id) == (usuario_admin.id, 'ola mal lanzada', t.id)
        assert f.antes['estado'] == 'EN_PROCESO' and f.antes['operario_id'] == usuario.id
        assert f.despues == {'estado': 'CANCELADO'}
        assert f.origen == f'PUT /api/picking/{t.id}/cancelar'

    def test_reabrir_conserva_al_operario_y_lo_recogido(self, client, db, h, usuario,
                                                        almacen, producto, ub):
        t = _tarea_picking(db, almacen, producto, ub, operario_id=usuario.id,
                           estado='BLOQUEADO', cantidad_recogida=3,
                           motivo_bloqueo='FALTANTE')
        assert client.put(f'/api/picking/{t.id}/reabrir', json={}, headers=h).status_code == 400
        r = client.put(f'/api/picking/{t.id}/reabrir', json={'motivo': 'apareció'}, headers=h)
        assert r.status_code == 200
        t = db.session.get(type(t), t.id)
        assert t.operario_id is None and t.ultimo_operario_id == usuario.id
        [f] = _filas(accion='REABRIR')
        assert f.antes['cantidad_recogida'] == 3 and f.despues['cantidad_recogida'] == 0

    def test_reportar_problema_no_pierde_quien(self, db, usuario, almacen, producto, ub):
        from app.services.picking_service import PickingService
        t = _tarea_picking(db, almacen, producto, ub, operario_id=usuario.id,
                           estado='EN_PROCESO')
        PickingService.reportar_problema(t.id, usuario.id, 'FALTANTE',
                                         cantidad_encontrada=0, observaciones='vacío')
        t = db.session.get(type(t), t.id)
        assert t.operario_id is None and t.ultimo_operario_id == usuario.id
        [f] = _filas(accion='BLOQUEAR')
        assert f.usuario_id == usuario.id and f.motivo == 'FALTANTE: vacío'

    def test_purgar_exige_motivo_y_guarda_cada_fila(self, client, db, h, almacen,
                                                    producto, ub):
        t = _tarea_picking(db, almacen, producto, ub, estado='COMPLETADO',
                           cantidad_recogida=0)
        assert client.delete('/api/picking/purgar-ceros', headers=h).status_code == 400
        r = client.delete('/api/picking/purgar-ceros?motivo=confirms+fallidos', headers=h)
        assert r.get_json()['eliminadas'] == 1
        [f] = _filas(accion='ELIMINAR', entidad='TareaPicking')
        assert f.entidad_codigo == t.codigo and f.antes['codigo'] == t.codigo
        assert f.motivo == 'confirms fallidos'


def _packing(db, almacen, **kw):
    import uuid
    from app.models.packing import TareaPacking
    t = TareaPacking(codigo=f'PKG-{uuid.uuid4().hex[:6].upper()}', almacen_id=almacen.id,
                     numero_pedido_siesa='PD-BIT', estado=kw.pop('estado', 'VERIFICADO'),
                     **kw)
    db.session.add(t)
    db.session.flush()
    return t


def _bulto(db, tarea, n=1, estado='PENDIENTE'):
    from app.models.bulto import Bulto
    b = Bulto(tarea_id=tarea.id, codigo_barras=f'{tarea.codigo}-{n:02d}', tipo='Caja',
              numero=n, total=1, estado=estado)
    db.session.add(b)
    db.session.flush()
    return b


class TestPacking:

    def test_cancelar_exige_motivo(self, client, db, h, almacen):
        t = _packing(db, almacen)
        db.session.commit()
        assert client.put(f'/api/packing/{t.id}/cancelar', json={}, headers=h).status_code == 400

    def test_cancelar_guarda_bultos_y_no_pisa_observaciones(self, client, db, h,
                                                            usuario_admin, almacen):
        t = _packing(db, almacen, observaciones='frágil')
        b = _bulto(db, t)
        codigo_bulto = b.codigo_barras
        db.session.commit()
        r = client.put(f'/api/packing/{t.id}/cancelar',
                       json={'motivo': 'pedido anulado'}, headers=h)
        assert r.status_code == 200
        assert db.session.get(type(t), t.id).observaciones == 'frágil | Cancelado: pedido anulado'
        [fb] = _filas(accion='ELIMINAR', entidad='Bulto')
        assert fb.antes['codigo_barras'] == codigo_bulto and fb.usuario_id == usuario_admin.id
        [fc] = _filas(accion='CANCELAR', entidad='TareaPacking')
        assert fc.antes['observaciones'] == 'frágil'

    def test_resetear_guarda_el_error_de_siesa(self, client, db, h, almacen):
        t = _packing(db, almacen, siesa_response='rechazo 42')
        _bulto(db, t)
        db.session.commit()
        assert client.post(f'/api/packing/{t.id}/resetear-siesa', json={},
                           headers=h).status_code == 200
        [f] = _filas(accion='REABRIR', entidad='TareaPacking')
        assert f.antes['siesa_response'] == 'rechazo 42'
        assert len(_filas(accion='ELIMINAR', entidad='Bulto')) == 1

    def test_el_cierre_guarda_quien_cerro(self, db, almacen, usuario):
        from tests.flujo import conductor_de_flujo as cf
        from app.models.packing import TareaPacking
        productos, _ = cf.sembrar_catalogo(db, almacen)
        pedido = cf.sembrar_pedido(db, productos)
        f = cf.Flujo(pedido=pedido, almacen_id=almacen.id, usuario_id=usuario.id,
                     producto_ids=[p.id for p in productos])
        cf.hacer_picking(db, f, 10, 7)
        cf.hacer_packing(db, f)
        assert TareaPacking.query.get(f.packing_id).cerrado_por_id == usuario.id


def _job_fallido(db):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob(tipo='DESPACHO_F470', payload='{}', estado='FALLIDO', intentos=3,
                 error_ultimo='el documento de cruce no existe')
    db.session.add(j)
    db.session.commit()
    return j


class TestJobs:

    def test_reintentar_guarda_el_error_que_borra(self, client, db, h, usuario_admin):
        j = _job_fallido(db)
        r = client.post(f'/api/reposicion/siesa-jobs/{j.id}/reintentar', json={}, headers=h)
        assert r.status_code == 200
        [f] = _filas(accion='REINTENTAR')
        assert f.antes['error_ultimo'] == 'el documento de cruce no existe'
        assert f.antes['intentos'] == 3 and f.usuario_id == usuario_admin.id
        assert db.session.get(type(j), j.id).error_ultimo is None

    def test_resetear_en_lote_tambien(self, client, db, h):
        _job_fallido(db)
        _job_fallido(db)
        r = client.post('/api/siesa/resetear-jobs-fallidos?tipo=DESPACHO_F470', headers=h)
        assert r.get_json()['reseteados'] == 2
        assert len(_filas(accion='REINTENTAR')) == 2


class TestLayout:

    def test_eliminar_ubicacion_guarda_el_hueco(self, client, db, h, almacen, producto, ub):
        from app.models.inventario import UbicacionProducto
        db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=producto.id,
                                         cantidad=0))
        db.session.commit()
        r = client.delete(f'/api/almacenes/ubicaciones/{ub.id}', headers=h)
        assert r.status_code == 200
        [f] = _filas(accion='ELIMINAR', entidad='Ubicacion')
        assert f.antes['codigo'] == 'BIT-PIK-1'
        assert f.antes['asignaciones'][0]['producto_id'] == producto.id

    def test_forzar_guarda_cada_tarea_que_se_lleva(self, client, db, h, almacen,
                                                   producto, ub):
        codigo = _tarea_picking(db, almacen, producto, ub, estado='COMPLETADO').codigo
        r = client.delete(f'/api/almacenes/ubicaciones/{ub.id}?forzar=true', headers=h)
        assert r.status_code == 200
        [f] = _filas(accion='ELIMINAR', entidad='TareaPicking')
        assert f.entidad_codigo == codigo and f.antes['estado'] == 'COMPLETADO'


class TestJuicios:

    def test_retractar_anula_y_no_borra(self, client, db, h, usuario_admin):
        from app.models.juicio_temporada import JuicioTemporada
        url = '/api/kardex/temporada/juicios'
        client.post(url, json={'referencia': 'REF1', 'cantidad_juicio': 10}, headers=h)
        client.post(url, json={'referencia': 'REF1', 'cantidad_juicio': 12}, headers=h)
        [fe] = _filas(accion='EDITAR', entidad='JuicioTemporada')
        assert fe.antes['cantidad_juicio'] == 10 and fe.despues['cantidad_juicio'] == 12

        r = client.post(url, json={'referencia': 'REF1', 'cantidad_juicio': None,
                                   'motivo': 'el comité se retractó'}, headers=h)
        assert r.get_json()['anulado'] is True
        j = JuicioTemporada.query.filter_by(referencia='REF1').one()
        assert j.anulado_en is not None and j.anulado_por_id == usuario_admin.id
        assert j.cantidad_juicio == 12           # el juicio sigue ahí
        assert 'REF1' not in client.get(url + '?temporada=2026-27', headers=h).get_json()['juicios']
        [fa] = _filas(accion='ANULAR')
        assert fa.motivo == 'el comité se retractó'


class TestMaestros:

    def test_mapeo_borrado_deja_la_fila(self, client, db, h):
        from app.models.siesa_mapeo_unidades import SiesaMapeoUnidades
        m = SiesaMapeoUnidades(tipo_inv_siesa='INV-BIT', unidad_negocio_id='001')
        db.session.add(m)
        db.session.commit()
        assert client.delete(f'/api/config/mapeo-unidades/{m.id}', headers=h).status_code == 200
        [f] = _filas(accion='ELIMINAR', entidad='SiesaMapeoUnidades')
        assert f.antes['unidad_negocio_id'] == '001'

    def test_baja_de_producto_conductor_y_vehiculo(self, client, db, h, producto):
        import uuid
        from app.models.conductor import Conductor
        from app.models.vehiculo import Vehiculo
        c = Conductor(nombre='C', cedula=f'C-{uuid.uuid4().hex[:6]}', activo=True)
        v = Vehiculo(placa='BIT123', tipo='Turbo', activo=True)
        db.session.add_all([c, v])
        db.session.commit()
        client.delete(f'/api/productos/{producto.id}', json={'motivo': 'descontinuado'}, headers=h)
        client.delete(f'/api/rutas/conductores/{c.id}', headers=h)
        client.put(f'/api/rutas/vehiculos/{v.id}', json={'activo': False, 'motivo': 'vendido'},
                   headers=h)
        bajas = {f.entidad: f for f in _filas(accion='DESACTIVAR')}
        assert set(bajas) == {'Producto', 'Conductor', 'Vehiculo'}
        assert bajas['Producto'].motivo == 'descontinuado'
        assert bajas['Vehiculo'].motivo == 'vendido'
        # Editar sin tocar `activo` no es una baja.
        client.put(f'/api/rutas/vehiculos/{v.id}', json={'tipo': 'NHR'}, headers=h)
        assert len(_filas(accion='DESACTIVAR')) == 3

    def test_ruta_maestra_editada_y_eliminada(self, client, db, h):
        from app.models.ruta_maestra import RutaMaestra, RutaMaestraParada
        m = RutaMaestra(nombre='BIT', tipo_ruta='Urbana', activa=True)
        db.session.add(m)
        db.session.flush()
        db.session.add(RutaMaestraParada(ruta_maestra_id=m.id, municipio='NEIVA', orden=1))
        db.session.commit()
        client.put(f'/api/rutas/maestras/{m.id}', json={'paradas': ['PITALITO']}, headers=h)
        [fe] = _filas(accion='EDITAR', entidad='RutaMaestra')
        assert fe.antes == {'paradas': ['NEIVA']} and fe.despues == {'paradas': ['PITALITO']}
        assert client.delete(f'/api/rutas/maestras/{m.id}', headers=h).status_code == 200
        [fd] = _filas(accion='ELIMINAR', entidad='RutaMaestra')
        assert fd.antes['paradas'][0]['municipio'] == 'PITALITO'


class TestCancelacionesSinPantalla:
    """Rutas sin UI (DEUDA_SIN_UI): el motivo es obligatorio."""

    def test_recepcion(self, client, db, h, almacen):
        from app.models.recepcion import RecepcionMercancia
        r0 = RecepcionMercancia(codigo='REC-BIT', numero_oc_siesa='OC-1',
                                almacen_id=almacen.id, observaciones='parcial')
        db.session.add(r0)
        db.session.commit()
        url = f'/api/recepcion/{r0.id}/cancelar'
        assert client.put(url, json={}, headers=h).status_code == 400
        assert client.put(url, json={'motivo': 'OC anulada'}, headers=h).status_code == 200
        [f] = _filas(accion='CANCELAR', entidad='RecepcionMercancia')
        assert f.antes['observaciones'] == 'parcial'
        assert db.session.get(RecepcionMercancia, r0.id).observaciones == 'parcial | Cancelada: OC anulada'

    def test_traslado(self, client, db, h, usuario_admin):
        from app.models.traslado import SolicitudTraslado
        s = SolicitudTraslado(codigo='ST-BIT-1', bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NC1', estado='ENVIADA',
                              solicitante_id=usuario_admin.id)
        db.session.add(s)
        db.session.commit()
        url = f'/api/traslados/{s.id}/cancelar'
        assert client.post(url, json={}, headers=h).status_code == 400
        assert client.post(url, json={'motivo': 'ya no hace falta'}, headers=h).status_code == 200
        [f] = _filas(accion='CANCELAR', entidad='SolicitudTraslado')
        assert f.antes['estado'] == 'ENVIADA' and f.motivo == 'ya no hace falta'

    def test_reposicion(self, client, db, h, almacen, producto, ub_reserva, ub_picking):
        from app.models.tarea_reposicion import TareaReposicion
        t = TareaReposicion(codigo='REP-BIT', producto_id=producto.id, almacen_id=almacen.id,
                            cantidad_unidades=5, ubicacion_reserva_id=ub_reserva.id,
                            ubicacion_picking_id=ub_picking.id, estado='PENDIENTE')
        db.session.add(t)
        db.session.commit()
        url = f'/api/reposicion/cancelar/{t.id}'
        assert client.post(url, json={}, headers=h).status_code == 400
        assert client.post(url, json={'motivo': 'LPN dañado'}, headers=h).status_code == 200
        assert _filas(accion='CANCELAR', entidad='TareaReposicion')[0].motivo == 'LPN dañado'

    def test_devolucion_de_cliente(self, client, db, h, usuario, almacen):
        from app.models.devolucion_cliente import DevolucionCliente
        t = _packing(db, almacen, estado='DESPACHADO')
        d = DevolucionCliente(codigo='DEVC-BIT', tarea_packing_id=t.id,
                              numero_pedido_siesa='PD-BIT', almacen_id=almacen.id,
                              tipo_docto_fe='FE', consec_fe='1',
                              recepcionista_id=usuario.id)
        db.session.add(d)
        db.session.commit()
        url = f'/api/devoluciones/{d.id}/cancelar'
        assert client.post(url, json={}, headers=h).status_code == 400
        assert client.post(url, json={'motivo': 'no llegó'}, headers=h).status_code == 200
        # Quien la atendió no se reemplaza por quien canceló.
        assert db.session.get(DevolucionCliente, d.id).recepcionista_id == usuario.id
        assert _filas(accion='CANCELAR', entidad='DevolucionCliente')[0].motivo == 'no llegó'


class TestMuelle:

    def test_asignar_desasignar_y_cargar_dejan_autor(self, client, db, h, usuario_admin,
                                                    almacen):
        from app.models.bulto import Bulto
        from app.models.ruta_despacho import RutaDespacho
        from datetime import datetime
        # Despachable: RM + FE confirmadas (m048fiscal), no solo la bandera.
        t = _packing(db, almacen, estado='DESPACHADO', siesa_triggered=True,
                     rm_tipo='RM', rm_consec=9, fe_confirmada_at=datetime.utcnow())
        b = _bulto(db, t)
        ruta = RutaDespacho(conductor_id=1, tipo_ruta='Urbana', estado='EN_CARGUE')
        db.session.add(ruta)
        db.session.commit()
        client.post('/api/muelle/asignar', json={'ruta_id': ruta.id, 'bultos_ids': [b.id]},
                    headers=h)
        b = db.session.get(Bulto, b.id)
        assert b.asignado_ruta_por_id == usuario_admin.id and b.asignado_ruta_at

        assert client.delete(f'/api/muelle/desasignar/{b.id}', headers=h).status_code == 200
        [f] = _filas(accion='DESASIGNAR')
        assert f.antes['ruta_despacho_id'] == ruta.id
        assert f.antes['asignado_ruta_por_id'] == usuario_admin.id

        client.post('/api/muelle/asignar', json={'ruta_id': ruta.id, 'bultos_ids': [b.id]},
                    headers=h)
        r = client.post(f'/api/muelle/cargar/{b.codigo_barras}', json={'ruta_id': ruta.id},
                        headers=h)
        assert r.status_code == 200
        assert db.session.get(Bulto, b.id).cargado_por_id == usuario_admin.id


@pytest.fixture
def actores(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('operario', 'op_bit@test.com'), ('conductor', 'cond_bit@test.com')):
        u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.flush()
        out[rol] = u
    db.session.commit()
    return out


class TestRuta:

    def _ruta_en_transito(self, db, almacen, actores):
        from tests.flujo import conductor_de_flujo as cf
        productos, _ = cf.sembrar_catalogo(db, almacen)
        pedido = cf.sembrar_pedido(db, productos)
        f = cf.Flujo(pedido=pedido, almacen_id=almacen.id, usuario_id=actores['operario'].id,
                     producto_ids=[p.id for p in productos])
        cf.hacer_picking(db, f, 10, 7)
        cf.hacer_packing(db, f)
        cf.hacer_ruta(db, f, actores['conductor'].id)
        return f

    def test_forzar_cierre_exige_motivo_y_no_pisa_la_salida(self, client, db, h,
                                                            usuario_admin, almacen, actores):
        from datetime import datetime
        from app.models.ruta_despacho import RutaDespacho
        f = self._ruta_en_transito(db, almacen, actores)
        salida = datetime(2026, 9, 24, 13, 0, 0)
        ruta = db.session.get(RutaDespacho, f.ruta_id)
        ruta.fecha_cierre = salida
        db.session.commit()
        url = f'/api/rutas/{f.ruta_id}/forzar-cierre'
        assert client.post(url, json={}, headers=h).status_code == 400
        r = client.post(url, json={'motivo': 'conductor sin señal'}, headers=h)
        assert r.status_code == 200
        ruta = db.session.get(RutaDespacho, f.ruta_id)
        assert ruta.fecha_cierre == salida            # la hora de salida se conserva
        assert ruta.fecha_entregada is not None
        # P1-5 (2026-09-25): el cierre forzado deja la ruta ENTREGADA, sin
        # liquidar — liquidarla saltaba las guardas de `liquidar_ruta`.
        assert ruta.estado == 'ENTREGADA'
        assert ruta.estado_financiero != 'LIQUIDADA' and ruta.liquidada_en is None
        [ff] = _filas(accion='FORZAR')
        assert ff.motivo == 'conductor sin señal'
        assert ff.despues['paradas_auto_rechazadas'] == [f.packing_id]
        assert _filas(accion='LIQUIDAR') == []

    def test_liquidar_guarda_fecha_y_autor(self, client, db, h, usuario_admin, almacen,
                                           actores, monkeypatch):
        from app.models.ruta_despacho import RutaDespacho
        from app.services import liquidacion_service as ls
        monkeypatch.setattr(ls.LiquidacionService, 'crear_devoluciones_pendientes_ruta',
                            staticmethod(lambda _id: {'creadas': 0}))
        f = self._ruta_en_transito(db, almacen, actores)
        cf_entrega = __import__('tests.flujo.conductor_de_flujo', fromlist=['x'])
        cf_entrega.hacer_entrega(db, f)
        r = client.post(f'/api/rutas/{f.ruta_id}/liquidar', headers=h)
        assert r.status_code == 200, r.get_json()
        ruta = db.session.get(RutaDespacho, f.ruta_id)
        assert ruta.liquidada_por_id == usuario_admin.id and ruta.liquidada_en
        [fl] = _filas(accion='LIQUIDAR')
        assert fl.antes['estado_financiero'] != 'LIQUIDADA'

    def test_reconfirmar_guarda_lo_que_pisa(self, db, almacen, actores):
        from tests.flujo import conductor_de_flujo as cf
        from app.services.ruta_service import RutaService
        f = self._ruta_en_transito(db, almacen, actores)
        cf.hacer_entrega(db, f, monto_cobrado=1000)
        RutaService.confirmar_parada(f.ruta_id, f.packing_id, actores['conductor'].id,
                                     {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                                      'monto_cobrado': 900})
        db.session.commit()
        [fe] = _filas(accion='EDITAR', entidad='RecaudoEntrega')
        assert float(fe.antes['monto_cobrado']) == 1000
        assert float(fe.despues['monto_cobrado']) == 900
        assert fe.usuario_id == actores['conductor'].id


class TestLectura:

    def test_solo_gestion(self, client, db, jwt_token):
        r = client.get('/api/analitica/bitacora',
                       headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403

    def test_filtra_y_pagina(self, client, db, h, usuario_admin):
        from app.services.bitacora import registrar_accion
        from app.utils.fecha import dia_operativo
        for i in range(3):
            registrar_accion('CANCELAR', 'TareaPicking', i, usuario_id=usuario_admin.id,
                             motivo=f'm{i}', almacen_id=7)
        registrar_accion('ELIMINAR', 'Bulto', 9)
        db.session.commit()
        r = client.get('/api/analitica/bitacora?accion=CANCELAR&per_page=2', headers=h).get_json()
        assert r['total'] == 3 and len(r['acciones']) == 2
        assert r['acciones'][0]['motivo'] == 'm2'          # la más reciente primero
        hoy = dia_operativo().isoformat()
        assert client.get(f'/api/analitica/bitacora?dia={hoy}&entidad=Bulto',
                          headers=h).get_json()['total'] == 1
        assert client.get(f'/api/analitica/bitacora?usuario_id={usuario_admin.id}&almacen_id=7',
                          headers=h).get_json()['total'] == 3
        assert client.get('/api/analitica/bitacora?accion=BORRAR', headers=h).status_code == 400
        assert client.get('/api/analitica/bitacora?dia=ayer', headers=h).status_code == 400


class TestSobreviveAlCorte:

    def test_protegida_e_irrecuperable(self):
        import importlib.util
        for nombre in ('reset_transaccional', 'verificar_restauracion'):
            spec = importlib.util.spec_from_file_location(nombre, RAIZ / 'scripts' / f'{nombre}.py')
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if nombre == 'reset_transaccional':
                assert 'bitacora_acciones' in mod.PROTEGIDAS_ANALITICAS
                assert 'bitacora_acciones' not in mod.OPERATIVAS
            else:
                assert 'bitacora_acciones' in mod.IRRECUPERABLES

    def test_sin_claves_foraneas(self, app):
        """Con una FK hacia una operativa, el corte fallaría o se la llevaría."""
        from app.extensions import db
        assert not db.metadata.tables['bitacora_acciones'].foreign_keys
