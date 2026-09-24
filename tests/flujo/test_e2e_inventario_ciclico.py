"""
Un día de inventario cíclico en la bodega, **por los endpoints HTTP reales**.

Los demás archivos de conteo arman el mundo llamando a `ConteoService`
directamente: prueban la política, no la puerta. Acá cada paso es lo que hace
la PWA o el panel —`/api/mobile/tarea-actual`, `/api/mobile/escanear`,
`/api/mobile/confirmar`, `/api/conteo/definitivos`, `/api/conteo/<id>/ajustar`,
el tablero del líder, las estadísticas, la auditoría— con un JWT por rol
(operario, jefe de almacén, supervisor, admin). Lo que un endpoint agrega o
quita por encima del servicio (un permiso, un campo de más en la respuesta, un
código de estado que la pantalla interpreta) solo se ve así.

Cada escenario es independiente y arma su propio mundo:

1. Plan del día: cupo, rezago, «Cancelar rezago» con vista previa = ejecución.
2. El operario cuenta: cola unificada, escaneo con factor, total tecleado,
   reintentos idempotentes, cierre sin cantidad y el cero confirmado.
3. Dentro de tolerancia: ajuste sin CC2, y el tope del ajuste automático.
4. Fuera de tolerancia: RECONTAR_TU a ciegas, CC2 doble ciego, CC3 del
   supervisor, aprobación por valor (supervisor / jefe sin tope / jefe con tope).
5. Venta durante el conteo: RECONTAR sin cifras, y al tercero MOVIMIENTO_CONTINUO.
6. «No lo encontré»: bloqueo, tablero, reabrir (doble ciego) o cancelar.
7. Cancelar un CC2 cancela la cadena y libera el hueco para el generador.
8. `/editar`: qué se corrige y qué no.
9. Tablero del líder: bloques y permisos contra la respuesta real.
10. Estadísticas que cuadran con lo que pasó.
11. La auditoría de conteo no ve BLOQUEA sobre el mundo sano.

Siesa es de mentira a nivel de la respuesta cruda de `API_v2_Inventarios_InvFecha`
(como `tests/test_conteo_teorico_pos.py`), pero por SKU: cada hueco tiene su fila.
Cero red, cero base real.
"""
import json
import re

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

# ─────────────────────────────────────────────────────────────────────────────
# Siesa de mentira, una fila por SKU
# ─────────────────────────────────────────────────────────────────────────────


class SiesaPorSku:
    """Filas de InvFecha por referencia. `poner` las cambia entre dos llamadas:
    así se simula la venta de caja que entra mientras el operario cuenta."""

    COSTO = 1000.0

    def __init__(self):
        self.filas = {}

    def poner(self, sku, existencia, pos=0, salida_sin_conf=None, costo=COSTO):
        ssc = pos if salida_sin_conf is None else salida_sin_conf
        fila = {
            'f120_referencia': sku, 'f150_id': 'NB1',
            'f400_cant_existencia_1': float(existencia),
            'f400_cant_comprometida_1': 0.0,
            'f400_cant_salida_sin_conf_1': float(ssc),
            'f400_cant_pos_1': float(pos),
            'f400_id_lote': '', 'f400_id_ubicacion_aux': None,
        }
        if costo is not None:
            fila['f400_costo_prom_uni'] = float(costo)
        self.filas[sku] = fila

    def respuesta(self, codigo, bodega=None):
        fila = self.filas.get(codigo)
        return {'detalle': {'Table': [dict(fila)] if fila else []}}


@pytest.fixture
def siesa(monkeypatch):
    """Parcheado sobre la CLASE (ver CLAUDE.md, refactor del gateway)."""
    from app.services.connekta_gateway import ConnektaGateway, connekta
    falsa = SiesaPorSku()
    if 'get_inventario_fecha' in vars(connekta):
        monkeypatch.delattr(connekta, 'get_inventario_fecha')
    monkeypatch.setattr(connekta, 'modo_simulacion', False)
    monkeypatch.setattr(ConnektaGateway, 'get_inventario_fecha',
                        lambda self, codigo, bodega=None: falsa.respuesta(codigo, bodega))
    return falsa


@pytest.fixture(autouse=True)
def _politica_por_defecto(monkeypatch):
    """Cada escenario declara la configuración que necesita; la del proceso no
    se hereda."""
    for nombre in ('CONTEO_CUPO_DIARIO', 'CONTEO_CUPO_POR_BODEGA', 'CONTEO_INTERVALOS_DIAS',
                   'CONTEO_WATCHDOG_DIAS_SIN_REABRIR', 'CONTEO_TOLERANCIA_UNIDADES',
                   'CONTEO_TOLERANCIA_PCT', 'CONTEO_TOLERANCIA_TOPE_VALOR',
                   'CONTEO_TOPE_AUTOAJUSTE', 'CONTEO_TOPE_APROBACION_JEFE'):
        monkeypatch.delenv(nombre, raising=False)
    monkeypatch.setenv('CONTEO_CUPO_DIARIO', '50')


# ─────────────────────────────────────────────────────────────────────────────
# La bodega: NB1, seis personas
# ─────────────────────────────────────────────────────────────────────────────

class Bodega:
    def __init__(self, app, client, db, siesa, almacen):
        self.app, self.client, self.db, self.siesa, self.almacen = app, client, db, siesa, almacen
        self._n = 0

    # ── personas ──────────────────────────────────────────────────────────
    def persona(self, email, rol, **kw):
        from app.models.usuario import Usuario
        u = Usuario(nombre=email.split('@')[0], email=email,
                    password_hash=generate_password_hash('x'), rol=rol,
                    almacen_id=self.almacen.id, activo=True, **kw)
        self.db.session.add(u)
        self.db.session.commit()
        return u

    # ── huecos ────────────────────────────────────────────────────────────
    def hueco(self, clase='C', teorico=100, *, pos=0, salida_sin_conf=None, costo=1000.0,
              factor=None, ean_caja=None, stock_wms=None):
        """Un producto con su propio hueco en NB1, clasificado en ESTE almacén,
        y su fila en la Siesa de mentira (`teorico` = existencia − POS)."""
        from app.models.inventario import UbicacionProducto
        from app.models.producto import Producto
        from app.models.producto_clasificacion_abc import ProductoClasificacionABC
        from app.models.ubicacion import Ubicacion
        self._n += 1
        sku = f'E2E{self._n:03d}'
        prod = Producto(codigo=sku, nombre=f'Producto {sku}', codigo_siesa=sku,
                        codigo_barras=f'770{self._n:07d}', unidad_negocio_id='001',
                        factor_conversion=factor or 1,
                        codigo_barras_empaque=ean_caja,
                        unidad_empaque='CJA' if ean_caja else None, activo=True)
        self.db.session.add(prod)
        self.db.session.flush()
        ub = Ubicacion(codigo=f'E2E-UB-{self._n:03d}', almacen_id=self.almacen.id,
                       tipo_zona='GENERAL', stock_minimo=0, stock_maximo=99999,
                       secuencia_ruteo=self._n, activo=True)
        self.db.session.add(ub)
        self.db.session.flush()
        self.db.session.add(UbicacionProducto(
            ubicacion_id=ub.id, producto_id=prod.id,
            cantidad=stock_wms if stock_wms is not None else max(teorico, 1),
            reservado=0, bloqueado=0))
        self.db.session.add(ProductoClasificacionABC(
            producto_id=prod.id, almacen_id=self.almacen.id, clasificacion=clase))
        self.db.session.commit()
        self.siesa.poner(sku, existencia=teorico + pos, pos=pos,
                         salida_sin_conf=salida_sin_conf, costo=costo)
        return sku

    # ── HTTP ──────────────────────────────────────────────────────────────
    def h(self, usuario):
        with self.app.app_context():
            return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}

    def get(self, usuario, url):
        r = self.client.get(url, headers=self.h(usuario))
        return r.status_code, r.get_json()

    def post(self, usuario, url, body=None):
        r = self.client.post(url, json=body or {}, headers=self.h(usuario))
        return r.status_code, r.get_json()

    def put(self, usuario, url, body=None):
        r = self.client.put(url, json=body or {}, headers=self.h(usuario))
        return r.status_code, r.get_json()

    # ── el plan y los conteos, como los hace la gente ─────────────────────
    def programar(self, admin, clase):
        """El admin aprieta «Generar» de una clase (`/abc/generar-tareas`)."""
        st, r = self.post(admin, '/api/conteo/abc/generar-tareas',
                          {'almacen_id': self.almacen.id, 'clasificacion': clase})
        assert st == 201, r
        return r

    def raiz_de(self, sku):
        """La raíz viva más reciente del hueco de `sku`."""
        from app.models.conteo import SesionConteo
        from app.models.producto import Producto
        pid = Producto.query.filter_by(codigo=sku).one().id
        self.db.session.expire_all()
        return (SesionConteo.query
                .filter_by(producto_id=pid, es_segundo_conteo=False)
                .order_by(SesionConteo.id.desc()).first())

    def sesion(self, sid):
        from app.models.conteo import SesionConteo
        self.db.session.expire_all()
        return self.db.session.get(SesionConteo, sid)

    def abrir(self, usuario, sid):
        """El operario abre una tarea puntual (`/api/conteo/<id>/tarea`, la que
        usa también el Conteo Definitivo)."""
        return self.get(usuario, f'/api/conteo/{sid}/tarea')

    def confirmar(self, usuario, sid, total, cero=False):
        body = {'tarea_id': sid, 'tipo': 'CONTEO', 'items_escaneados': [],
                'total_contado': total}
        if cero:
            body['cero_confirmado'] = True
        return self.post(usuario, '/api/mobile/confirmar', body)

    def contar(self, usuario, sid, total):
        st, r = self.abrir(usuario, sid)
        assert st == 200, r
        st, r = self.confirmar(usuario, sid, total, cero=(total == 0))
        assert st == 200, r
        return r

    def jobs_ajuste(self, sid):
        from app.models.siesa_job import SiesaJob
        return SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_tipo='SesionConteo',
                                        referencia_id=sid).all()


@pytest.fixture
def bodega(app, client, db, siesa, almacen):
    almacen.bodega_siesa_id = 'NB1'
    almacen.centro_op_siesa = '003'
    db.session.commit()
    b = Bodega(app, client, db, siesa, almacen)
    b.op_a = b.persona('ana@e2e.test', 'operario', puede_picar=True)
    b.op_b = b.persona('beto@e2e.test', 'operario', puede_picar=True)
    b.op_c = b.persona('caro@e2e.test', 'operario', puede_picar=True)
    b.supervisor = b.persona('sofi@e2e.test', 'supervisor')
    b.jefe = b.persona('jorge@e2e.test', 'jefe_almacen')
    b.admin = b.persona('adri@e2e.test', 'admin')
    return b


# ─────────────────────────────────────────────────────────────────────────────
# Lo que el operario NUNCA puede recibir: números de Siesa
# ─────────────────────────────────────────────────────────────────────────────

#: Claves que delatan la base contra la que se compara (o el resultado de
#: compararla). El conteo es ciego: nada de esto viaja a quien cuenta.
CLAVES_PROHIBIDAS = re.compile(
    r'existencia|teorico|te[oó]rico|diferencia|cant_pos|salida_sin_conf|costo|valor'
    r'|siesa_|movimiento|tolerancia|esperad|stock|inicio_siesa', re.I)


def _claves(obj, prefijo=''):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f'{prefijo}{k}'
            yield from _claves(v, f'{prefijo}{k}.')
    elif isinstance(obj, list):
        for v in obj:
            yield from _claves(v, prefijo)


def _textos(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _textos(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _textos(v)
    elif isinstance(obj, str):
        yield obj


def assert_ciego(respuesta, *numeros):
    """Ninguna clave delatora y ninguno de `numeros` (los de Siesa de ese
    hueco) escrito en un texto de la respuesta."""
    malas = [k for k in _claves(respuesta) if CLAVES_PROHIBIDAS.search(k.split('.')[-1])]
    assert not malas, f'la respuesta al operario trae {malas}: {respuesta}'
    for texto in _textos(respuesta):
        for n in numeros:
            assert not re.search(rf'(?<![\d.]){re.escape(str(n))}(?![\d])', texto), \
                f'la respuesta al operario dice {n!r}: {texto!r}'


# ═════════════════════════════════════════════════════════════════════════════
# 1 · El plan del día
# ═════════════════════════════════════════════════════════════════════════════

class TestElPlanDelDia:

    def test_cupo_rezago_y_cancelar_rezago(self, bodega, monkeypatch):
        from app.models.conteo import EstadoConteo, SesionConteo
        b = bodega
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '3')
        for clase in ('A', 'A', 'B', 'C', 'C'):
            b.hueco(clase)

        # Día 1: el generador crea el cupo, ni uno más, A primero.
        st, r = b.post(b.admin, '/api/conteo/abc/generar-todas', {'almacen_id': b.almacen.id})
        assert st == 201, r
        assert r['total_tareas_creadas'] == 3, r
        assert r['plan']['omitidos_por_cupo'] == 2, r['plan']
        creadas = SesionConteo.query.filter_by(almacen_id=b.almacen.id,
                                               estado=EstadoConteo.PENDIENTE).all()
        assert sorted(s.clasificacion_abc for s in creadas) == ['A', 'A', 'B']

        # El mismo día otra vez: el cupo de hoy ya está cubierto — y lo dice.
        st, r = b.post(b.admin, '/api/conteo/abc/generar-todas', {'almacen_id': b.almacen.id})
        assert st == 201 and r['total_tareas_creadas'] == 0, r
        assert r['plan']['cupo']['motivo'] == 'CUPO_CUBIERTO', r['plan']['cupo']

        # El rezago de abril: con ≥ 2 días de cupo pendientes el generador se detiene.
        viejos = []
        for _ in range(4):
            sku = b.hueco('C')
            viejos.append(sku)
        from datetime import datetime, timedelta
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        for i, sku in enumerate(viejos):
            p = Producto.query.filter_by(codigo=sku).one()
            u = Ubicacion.query.filter_by(codigo=f'E2E-UB-{int(sku[3:]):03d}').one()
            b.db.session.add(SesionConteo(
                codigo=f'ABRIL-{i}', tipo='DIARIO_ABC', clasificacion_abc='C',
                ubicacion_id=u.id, almacen_id=b.almacen.id, producto_id=p.id,
                producto_codigo_siesa=sku, estado='PENDIENTE', es_segundo_conteo=False,
                fecha_creacion=datetime.utcnow() - timedelta(days=150)))
        # Lo que «Cancelar rezago» NO debe tocar: un conteo manual y uno ya asignado.
        st, r = b.post(b.supervisor, '/api/conteo/manual',
                       {'almacen_id': b.almacen.id, 'producto_codigo': b.hueco('B')})
        assert st == 201, r
        asignado = creadas[0]
        asignado.operario_id = b.op_a.id
        b.db.session.commit()

        otro = b.hueco('A')     # vencido, nunca contado: el generador lo querría
        st, r = b.post(b.admin, '/api/conteo/abc/generar-todas', {'almacen_id': b.almacen.id})
        assert st == 201 and r['total_tareas_creadas'] == 0, r
        assert r['plan']['cupo']['motivo'] == 'REZAGO', r['plan']['cupo']
        assert b.raiz_de(otro) is None

        # El tablero del líder lo avisa y le ofrece el botón solo al admin.
        st, t = b.get(b.supervisor, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert st == 200 and t['rezago']['generador_detenido'] and t['rezago']['hay_aviso'], t['rezago']
        assert t['permisos']['cancelar_rezago'] is False
        st, t_admin = b.get(b.admin, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert t_admin['permisos']['cancelar_rezago'] is True

        # Vista previa: exactamente lo cancelable, y lo que no se toca con su motivo.
        vivas_antes = {s.id: s.estado for s in SesionConteo.query.all()}
        st, prev = b.get(b.admin, f'/api/conteo/abc/limpiar-pendientes/preview?almacen_id={b.almacen.id}')
        assert st == 200, prev
        assert prev['ejecutado'] is False
        assert prev['a_cancelar'] == 6, prev          # 4 de abril + 2 del plan de hoy sin dueño
        assert prev['no_se_tocan'] == {'conteo_manual': 1, 'asignada_a_un_operario': 1}, prev
        assert t['rezago']['a_cancelar'] == prev['a_cancelar']
        b.db.session.expire_all()
        assert {s.id: s.estado for s in SesionConteo.query.all()} == vivas_antes, \
            'la vista previa tocó la base'

        # Si el rezago cambió desde la vista previa, no se cancela nada (409).
        st, r = b.post(b.admin, '/api/conteo/abc/limpiar-pendientes',
                       {'almacen_id': b.almacen.id, 'motivo': 'rezago de abril',
                        'esperadas': prev['a_cancelar'] + 1})
        assert st == 409, r
        b.db.session.expire_all()
        assert {s.id: s.estado for s in SesionConteo.query.all()} == vivas_antes

        # Solo el admin cancela; sin motivo no.
        st, _ = b.post(b.supervisor, '/api/conteo/abc/limpiar-pendientes',
                       {'almacen_id': b.almacen.id, 'motivo': 'x', 'esperadas': prev['a_cancelar']})
        assert st == 403
        st, _ = b.post(b.admin, '/api/conteo/abc/limpiar-pendientes',
                       {'almacen_id': b.almacen.id, 'esperadas': prev['a_cancelar']})
        assert st == 400

        # Ejecución = vista previa.
        st, r = b.post(b.admin, '/api/conteo/abc/limpiar-pendientes',
                       {'almacen_id': b.almacen.id, 'motivo': 'rezago de abril',
                        'esperadas': prev['a_cancelar']})
        assert st == 200, r
        assert r['canceladas'] == prev['a_cancelar'] and r['ejecutado'] is True
        for campo in ('por_clase', 'por_tipo', 'no_se_tocan'):
            assert r[campo] == prev[campo], campo
        b.db.session.expire_all()
        canceladas = SesionConteo.query.filter_by(estado=EstadoConteo.CANCELADO).all()
        assert len(canceladas) == prev['a_cancelar']
        assert all('rezago de abril' in s.motivo_edicion and s.editado_por == b.admin.id
                   for s in canceladas)
        assert SesionConteo.query.filter_by(tipo='MANUAL').one().estado == EstadoConteo.PENDIENTE
        assert b.sesion(asignado.id).estado == EstadoConteo.PENDIENTE

        # Sin rezago, el generador vuelve a andar — dentro del cupo.
        st, r = b.post(b.admin, '/api/conteo/abc/generar-todas', {'almacen_id': b.almacen.id})
        assert st == 201, r
        assert r['total_tareas_creadas'] == 1, r['plan']     # cupo 3 − 2 vivas (manual + asignada)
        assert r['por_clase']['A']['tareas_creadas'] == 1, 'nunca contadas de A primero'


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El operario cuenta
# ═════════════════════════════════════════════════════════════════════════════

class TestElOperarioCuenta:

    def test_cola_escaneo_factor_teclado_reintentos_y_cierre(self, bodega):
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('C', teorico=40, factor=12, ean_caja='CAJA-12')
        b.programar(b.admin, 'C')
        sid = b.raiz_de(sku).id

        # La cola unificada le da el conteo (no hay picking ni reposición).
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert st == 200, t
        assert (t['tipo'], t['id'], t['estado']) == ('CONTEO', sid, EstadoConteo.EN_PROCESO), t
        assert t['factor_conversion'] == 12 and t['cantidad_escaneada'] == 0
        assert_ciego(t, 40)
        s = b.sesion(sid)
        assert s.operario_id == b.op_a.id and s.foto_inicio_at is not None, \
            'toda apertura toma la foto de inicio'
        # Pedirla otra vez devuelve la misma, no otra.
        st, t2 = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t2['id'] == sid

        esc = lambda codigo, previo: b.post(b.op_a, '/api/mobile/escanear', {  # noqa: E731
            'codigo': codigo, 'tarea_id': sid, 'tipo': 'CONTEO', 'total_previo': previo})

        # Una unidad por su EAN.
        st, r = esc(f'770{int(sku[3:]):07d}', 0)
        assert st == 200 and r['cantidad_contada'] == 1, r
        # Una caja: vale el factor.
        st, r = esc('CAJA-12', 1)
        assert st == 200 and (r['cantidad_contada'], r['es_empaque']) == (13, True), r
        # La respuesta de la caja se perdió y la PWA reintenta lo mismo: no suma doble.
        st, r = esc('CAJA-12', 1)
        assert st == 200 and r['cantidad_contada'] == 13, r
        assert b.sesion(sid).cantidad_fisica == 13
        # Una PWA vieja sin `total_previo` no suma a ciegas.
        st, r = b.post(b.op_a, '/api/mobile/escanear',
                       {'codigo': 'CAJA-12', 'tarea_id': sid, 'tipo': 'CONTEO'})
        assert st == 400 and 'desactualizada' in r['error'], r
        # Otro producto no cuenta.
        st, r = esc('OTRA-COSA', 13)
        assert st == 400, r
        # Otro operario no puede escribirle al conteo de Ana.
        st, r = b.post(b.op_b, '/api/mobile/escanear', {
            'codigo': 'CAJA-12', 'tarea_id': sid, 'tipo': 'CONTEO', 'total_previo': 13})
        assert st == 400, r
        assert b.sesion(sid).cantidad_fisica == 13

        # Teclea el total de la pila: se fija, y el reintento escribe lo mismo.
        for _ in range(2):
            st, r = b.post(b.op_a, '/api/mobile/conteo/total',
                           {'tarea_id': sid, 'total_acumulado': 40})
            assert st == 200 and r['cantidad_contada'] == 40, r
        assert b.sesion(sid).cantidad_fisica == 40

        # Cerrar sin decir cuánto: rechazado (nunca se deriva de lo guardado).
        st, r = b.post(b.op_a, '/api/mobile/confirmar',
                       {'tarea_id': sid, 'tipo': 'CONTEO', 'items_escaneados': []})
        assert st == 400 and 'Falta la cantidad' in r['error'], r
        # Cero sin confirmar: rechazado. Con `cero_confirmado` que no es `true`: también.
        st, r = b.confirmar(b.op_a, sid, 0)
        assert st == 400 and 'Contaste 0' in r['error'], r
        st, r = b.post(b.op_a, '/api/mobile/confirmar',
                       {'tarea_id': sid, 'tipo': 'CONTEO', 'total_contado': 0,
                        'cero_confirmado': 'si'})
        assert st == 400, r
        assert b.sesion(sid).estado == EstadoConteo.EN_PROCESO

        # Cierra con lo que declaró.
        st, r = b.confirmar(b.op_a, sid, 40)
        assert st == 200 and r['resultado'] == 'MATCH', r
        s = b.sesion(sid)
        assert (s.estado, s.cantidad_fisica, s.tolerancia_primer_conteo) == ('MATCH', 40, 'EXACTO')
        # El cierre repetido (doble toque, reintento de red) no reabre nada.
        st, r = b.confirmar(b.op_a, sid, 40)
        assert st == 400, r
        assert b.sesion(sid).estado == EstadoConteo.MATCH
        # Y la cola ya no tiene conteos para Ana.
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert st == 200 and t.get('sin_tareas'), t

    def test_el_cero_confirmado_es_un_dato(self, bodega):
        b = bodega
        sku = b.hueco('C', teorico=0, stock_wms=3)
        b.programar(b.admin, 'C')
        sid = b.raiz_de(sku).id
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t['id'] == sid
        st, r = b.confirmar(b.op_a, sid, 0, cero=True)
        assert st == 200 and r['resultado'] == 'MATCH', r
