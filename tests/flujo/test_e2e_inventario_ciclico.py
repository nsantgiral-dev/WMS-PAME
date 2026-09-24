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


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Dentro de tolerancia
# ═════════════════════════════════════════════════════════════════════════════

class TestDentroDeTolerancia:

    def test_se_ajusta_sin_segundo_conteo_y_el_tope_decide_si_sale_solo(self, bodega, monkeypatch):
        from app.models.conteo import EstadoConteo
        b = bodega
        # Tolerancia de valor holgada: acá decide el tope del AUTOMÁTICO, no la tolerancia.
        monkeypatch.setenv('CONTEO_TOLERANCIA_TOPE_VALOR', '1000000')
        monkeypatch.setenv('CONTEO_TOPE_AUTOAJUSTE', '2500')
        chico = b.hueco('C', teorico=100)          # C: max(1 und, 5 %) = 5 und
        grande = b.hueco('C', teorico=100)
        b.programar(b.admin, 'C')

        # −2 × $1.000 = $2.000 ≤ tope $2.500 → sale solo.
        sid = b.raiz_de(chico).id
        r = b.contar(b.op_a, sid, 98)
        assert r == {'resultado': 'DENTRO_TOLERANCIA', 'mensaje': 'Conteo registrado — gracias.',
                     'sesion_id': sid}, r
        s = b.sesion(sid)
        assert (s.estado, s.ajuste_por_tolerancia, s.hijo_conteo) == (EstadoConteo.AJUSTANDO, True, None)
        assert s.aprobador_id is None and s.tolerancia_primer_conteo == 'DENTRO'
        jobs = b.jobs_ajuste(sid)
        assert len(jobs) == 1
        p = json.loads(jobs[0].payload)
        assert (p['motivo_codigo'], p['cantidad'], p['bodega'], p['centro_op']) == ('AJ-SAL', 2, 'NB1', '003')

        # −3 × $1.000 = $3.000 > tope → dentro de tolerancia, pero NO sale solo.
        sid2 = b.raiz_de(grande).id
        r = b.contar(b.op_a, sid2, 97)
        assert r['resultado'] == 'DENTRO_TOLERANCIA', r
        s2 = b.sesion(sid2)
        assert (s2.estado, s2.ajuste_por_tolerancia) == (EstadoConteo.DESCUADRE, True)
        assert b.jobs_ajuste(sid2) == []
        # El líder lo ve como aprobable, con su motivo de «no salió solo».
        st, lista = b.get(b.supervisor, f'/api/conteo/?estado=DESCUADRE&almacen_id={b.almacen.id}')
        fila = next(x for x in lista['sesiones'] if x['id'] == sid2)
        assert fila['no_sale_solo']['codigo'] == 'SUPERA_TOPE', fila
        st, r = b.put(b.supervisor, f'/api/conteo/{sid2}/ajustar')
        assert st == 202, r
        assert len(b.jobs_ajuste(sid2)) == 1

    def test_la_respuesta_al_operario_es_ciega_aunque_no_salga_solo(self, bodega, monkeypatch):
        """El operario solo recibe «Conteo registrado». Ni el valor del ajuste
        ni las cifras de Siesa que explican por qué no salió solo."""
        b = bodega
        monkeypatch.setenv('CONTEO_TOLERANCIA_TOPE_VALOR', '1000000')
        monkeypatch.setenv('CONTEO_TOPE_AUTOAJUSTE', '2500')
        sku = b.hueco('C', teorico=100, pos=3, salida_sin_conf=5)   # salidas que no son POS
        otro = b.hueco('C', teorico=100)
        b.programar(b.admin, 'C')
        r = b.contar(b.op_a, b.raiz_de(sku).id, 98)
        assert r['resultado'] == 'DENTRO_TOLERANCIA', r
        assert_ciego(r, 100, 103, 98, 2, 3, 5)
        r = b.contar(b.op_a, b.raiz_de(otro).id, 97)
        assert r['resultado'] == 'DENTRO_TOLERANCIA', r
        assert_ciego(r, 100, 97, 3, 3000, '3.000', 2500, '2.500')


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Fuera de tolerancia: recuento propio, doble ciego, definitivo y firma
# ═════════════════════════════════════════════════════════════════════════════

def _cadena_hasta_definitivo(b, sku, cc1=45, cc2=47):
    """CC1 (Ana, con su recuento propio) ≠ CC2 (Beto) → CC3 en la cola del
    supervisor. Todo por HTTP. Devuelve `(raiz_id, cc2_id, cc3_id)`."""
    raiz = b.raiz_de(sku).id
    r = b.contar(b.op_a, raiz, cc1)
    assert r['resultado'] == 'RECONTAR_TU', r
    assert set(r) == {'resultado', 'mensaje', 'sesion_id'}, r
    st, r = b.confirmar(b.op_a, raiz, cc1)
    assert st == 200 and r['resultado'] == 'SEGUNDO_CONTEO', r
    cc2_id = r['segundo_conteo_id']
    assert b.sesion(cc2_id).operario_id == b.op_b.id
    r = b.contar(b.op_b, cc2_id, cc2)
    assert r['resultado'] == 'TERCER_CONTEO', r
    return raiz, cc2_id, r['tercer_conteo_id']


class TestFueraDeTolerancia:

    def test_recuento_propio_doble_ciego_definitivo_y_aprobacion(self, bodega, monkeypatch):
        from app.models.conteo import EstadoConteo
        b = bodega
        monkeypatch.setenv('CONTEO_TOPE_APROBACION_JEFE', '0')
        sku = b.hueco('A', teorico=50)            # A: 0 und / 0,5 % → 45 cae fuera
        b.programar(b.admin, 'A')
        raiz = b.raiz_de(sku).id

        # CC1: fuera de tolerancia → RECONTAR_TU, a ciegas, al mismo operario.
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t['id'] == raiz
        st, r = b.confirmar(b.op_a, raiz, 45)
        assert st == 200 and r['resultado'] == 'RECONTAR_TU', r
        assert set(r) == {'resultado', 'mensaje', 'sesion_id'}, r
        assert_ciego(r, 50, 45, 5)
        s = b.sesion(raiz)
        assert (s.estado, s.operario_id, s.cantidad_fisica) == (EstadoConteo.EN_PROCESO, b.op_a.id, None)
        # La cola se la devuelve a ella, desde cero.
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert (t['id'], t['cantidad_escaneada']) == (raiz, 0), t
        # Recuenta lo mismo: ahora sí, segundo conteo de OTRA persona.
        st, r = b.confirmar(b.op_a, raiz, 45)
        assert st == 200 and r['resultado'] == 'SEGUNDO_CONTEO', r
        assert_ciego(r, 50, 45, 5)
        cc2 = r['segundo_conteo_id']

        # Doble ciego: Ana no puede tomar, abrir, escanear ni cerrar el CC2.
        st, r = b.abrir(b.op_a, cc2)
        assert st == 400, r
        st, r = b.post(b.op_a, '/api/mobile/escanear', {
            'codigo': sku, 'tarea_id': cc2, 'tipo': 'CONTEO', 'total_previo': 0})
        assert st == 400, r
        st, r = b.confirmar(b.op_a, cc2, 45)
        assert st == 400, r
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t.get('sin_tareas'), t
        # Y el operario que lo recibe lo ve ciego: ni la cifra de Siesa ni la de Ana.
        st, t = b.get(b.op_b, '/api/mobile/tarea-actual')
        assert t['id'] == cc2 and t['cantidad_escaneada'] == 0, t
        assert_ciego(t, 50, 45)
        st, r = b.confirmar(b.op_b, cc2, 47)
        assert st == 200 and r['resultado'] == 'TERCER_CONTEO', r
        assert_ciego(r, 50, 45, 47)
        cc3 = r['tercer_conteo_id']
        assert b.sesion(raiz).estado == EstadoConteo.TERCER_CONTEO

        # El CC3 no es de nadie del piso: ni en la cola, ni por id.
        for op in (b.op_a, b.op_b, b.op_c):
            st, t = b.get(op, '/api/mobile/tarea-actual')
            assert t.get('sin_tareas'), (op.email, t)
            st, r = b.abrir(op, cc3)
            assert st == 400, r
        st, r = b.get(b.op_c, '/api/conteo/definitivos')
        assert st == 403
        # El supervisor lo ve en su cola, ciego.
        st, cola = b.get(b.supervisor, f'/api/conteo/definitivos?almacen_id={b.almacen.id}')
        assert st == 200 and [x['id'] for x in cola['pendientes']] == [cc3], cola
        assert_ciego(cola, 50, 45, 47)
        st, t = b.abrir(b.supervisor, cc3)
        assert st == 200 and t['cantidad_contada'] == 0, t
        assert_ciego(t, 50, 45, 47)
        st, r = b.post(b.supervisor, '/api/mobile/escanear', {
            'codigo': sku, 'tarea_id': cc3, 'tipo': 'CONTEO', 'total_previo': 0})
        assert st == 200 and r['cantidad_contada'] == 1, r
        st, r = b.post(b.supervisor, '/api/mobile/conteo/total', {'tarea_id': cc3, 'total_acumulado': 48})
        assert st == 200, r
        st, r = b.confirmar(b.supervisor, cc3, 48)
        assert st == 200 and r['resultado'] == 'DESCUADRE' and r['raiz_id'] == raiz, r
        assert r['ajuste_bloqueado'] is None, r
        s = b.sesion(raiz)
        assert (s.estado, s.cantidad_fisica, s.diferencia) == (EstadoConteo.DESCUADRE, 48, -2)
        assert b.jobs_ajuste(raiz) == []

        # El hijo nunca se aprueba: saldría dos veces a Siesa.
        st, r = b.put(b.supervisor, f'/api/conteo/{cc3}/ajustar')
        assert st == 400, r
        # Jefe con tope $0: 403, y el tablero no le pinta el botón.
        st, t = b.get(b.jefe, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert t['permisos']['aprobar_ajuste'] is False
        st, r = b.put(b.jefe, f'/api/conteo/{raiz}/ajustar')
        assert st == 403, r
        assert b.jobs_ajuste(raiz) == [] and b.sesion(raiz).estado == EstadoConteo.DESCUADRE
        # Jefe con tope por debajo del valor ($2.000): 403 con el motivo, y el
        # tablero se lo dice en la fila en vez de pintarle el botón.
        monkeypatch.setenv('CONTEO_TOPE_APROBACION_JEFE', '1500')
        st, t = b.get(b.jefe, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert t['permisos']['aprobar_ajuste'] is True
        fila = next(f for f in t['decisiones']['ajustes']['aprobables']['filas'] if f['id'] == raiz)
        assert fila['valor'] == 2000 and 'supera tu tope' in (fila['no_puede_aprobar'] or ''), fila
        st, r = b.put(b.jefe, f'/api/conteo/{raiz}/ajustar')
        assert st == 403 and 'supera tu tope' in r['error'], r
        assert b.jobs_ajuste(raiz) == []
        # Jefe con tope suficiente: aprueba.
        monkeypatch.setenv('CONTEO_TOPE_APROBACION_JEFE', '5000')
        st, t = b.get(b.jefe, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        fila = next(f for f in t['decisiones']['ajustes']['aprobables']['filas'] if f['id'] == raiz)
        assert fila['no_puede_aprobar'] is None, fila
        st, r = b.put(b.jefe, f'/api/conteo/{raiz}/ajustar')
        assert st == 202, r
        s = b.sesion(raiz)
        assert (s.estado, s.aprobador_id) == (EstadoConteo.AJUSTANDO, b.jefe.id)
        p = json.loads(b.jobs_ajuste(raiz)[0].payload)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 2)
        # Aprobar dos veces no encola dos veces.
        st, r = b.put(b.supervisor, f'/api/conteo/{raiz}/ajustar')
        assert st in (200, 202), r
        assert len(b.jobs_ajuste(raiz)) == 1

    def test_el_supervisor_aprueba_cualquier_monto(self, bodega, monkeypatch):
        from app.models.conteo import EstadoConteo
        b = bodega
        monkeypatch.setenv('CONTEO_TOPE_APROBACION_JEFE', '0')
        sku = b.hueco('A', teorico=500, costo=90000)
        b.programar(b.admin, 'A')
        raiz, _, cc3 = _cadena_hasta_definitivo(b, sku, 480, 490)
        b.contar(b.supervisor, cc3, 470)
        st, r = b.put(b.jefe, f'/api/conteo/{raiz}/ajustar')
        assert st == 403, r
        st, r = b.put(b.supervisor, f'/api/conteo/{raiz}/ajustar')
        assert st == 202, r
        assert b.sesion(raiz).estado == EstadoConteo.AJUSTANDO
        p = json.loads(b.jobs_ajuste(raiz)[0].payload)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 30)

    def test_cc1_igual_cc2_ajusta_solo_bajo_el_tope(self, bodega):
        """Fuera de tolerancia, CC2 confirma a CC1: la verdad de bodega sale sola."""
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('A', teorico=50)
        b.programar(b.admin, 'A')
        raiz = b.raiz_de(sku).id
        b.contar(b.op_a, raiz, 46)
        st, r = b.confirmar(b.op_a, raiz, 46)
        cc2 = r['segundo_conteo_id']
        r = b.contar(b.op_b, cc2, 46)
        assert r['resultado'] == 'DESCUADRE', r
        assert_ciego(r, 50, 4)
        assert b.sesion(raiz).estado == EstadoConteo.AJUSTANDO
        p = json.loads(b.jobs_ajuste(raiz)[0].payload)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-SAL', 4)


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Venta durante el conteo
# ═════════════════════════════════════════════════════════════════════════════

class TestVentaDuranteElConteo:

    def test_recontar_a_ciegas_y_al_tercero_movimiento_continuo(self, bodega):
        from app.models.conteo import EstadoConteo, MotivoBloqueoConteo
        b = bodega
        sku = b.hueco('C', teorico=30)
        b.programar(b.admin, 'C')
        sid = b.raiz_de(sku).id

        # Ana abre (foto de inicio: existencia 30, POS 0)…
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t['id'] == sid
        # …y mientras cuenta se vende 1 por caja.
        b.siesa.poner(sku, existencia=30, pos=1)
        st, r = b.confirmar(b.op_a, sid, 29)
        assert st == 200 and r['resultado'] == 'RECONTAR', r
        assert r['motivo'] == 'MOVIMIENTO_EN_SIESA'
        assert_ciego(r, 30, 29, 1)
        s = b.sesion(sid)
        assert (s.estado, s.operario_id, s.cantidad_fisica) == (EstadoConteo.EN_PROCESO, b.op_a.id, None)
        historial = s.lista_conteos_descartados()
        assert len(historial) == 1
        assert historial[0]['motivo'] == 'MOVIMIENTO_SIESA' and historial[0]['cantidad_fisica'] == 29
        assert 'cant_pos 0→1' in historial[0]['movimiento'], historial[0]
        # El líder sí lo ve, en la vista completa.
        st, lista = b.get(b.supervisor, f'/api/conteo/?almacen_id={b.almacen.id}')
        assert st == 200 and any(x['id'] == sid for x in lista['sesiones'])

        # Segunda venta durante el recuento: RECONTAR otra vez.
        b.siesa.poner(sku, existencia=30, pos=2)
        st, r = b.confirmar(b.op_a, sid, 28)
        assert r['resultado'] == 'RECONTAR', r
        assert_ciego(r, 30, 28, 2)
        # Tercera: ya no se insiste — queda para el líder.
        b.siesa.poner(sku, existencia=30, pos=3)
        st, r = b.confirmar(b.op_a, sid, 27)
        assert st == 200 and r['resultado'] == 'BLOQUEADO', r
        assert r['motivo_bloqueo'] == MotivoBloqueoConteo.MOVIMIENTO_CONTINUO
        assert_ciego(r, 30, 27, 3)
        s = b.sesion(sid)
        assert (s.estado, s.motivo_bloqueo) == (EstadoConteo.BLOQUEADO, MotivoBloqueoConteo.MOVIMIENTO_CONTINUO)
        assert len(s.lista_conteos_descartados()) == 3
        assert b.jobs_ajuste(sid) == []
        # Ana ya no la recibe.
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t.get('sin_tareas'), t

        # Está en la cola del líder y en el tablero.
        st, bl = b.get(b.supervisor, f'/api/conteo/bloqueados?almacen_id={b.almacen.id}')
        fila = next(x for x in bl['bloqueados'] if x['id'] == sid)
        assert fila['motivo_bloqueo'] == 'MOVIMIENTO_CONTINUO' and fila['nivel'] == 'CC1'
        st, t = b.get(b.supervisor, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert t['decisiones']['bloqueados']['por_motivo'] == {'MOVIMIENTO_CONTINUO': 1}
        # Un operario no reabre.
        st, r = b.post(b.op_b, f'/api/conteo/{sid}/reabrir', {})
        assert st == 403
        # El líder lo reabre en un momento quieto: vuelve a la cola desde cero.
        st, r = b.post(b.supervisor, f'/api/conteo/{sid}/reabrir', {'nota': 'ya cerró la caja'})
        assert st == 200, r
        s = b.sesion(sid)
        assert (s.estado, s.operario_id, s.cantidad_fisica, s.motivo_bloqueo) == (
            EstadoConteo.PENDIENTE, None, None, None)
        assert s.lista_conteos_descartados()[-1]['evento'] == 'REABIERTO'
        # Con la caja quieta, cuadra.
        st, t = b.get(b.op_c, '/api/mobile/tarea-actual')
        assert t['id'] == sid, t
        st, r = b.confirmar(b.op_c, sid, 27)
        assert st == 200 and r['resultado'] == 'MATCH', r

    def test_reabrir_devuelve_los_dos_recuentos(self, bodega):
        """Tras la reapertura, un movimiento vuelve a pedir RECONTAR (no bloquea
        al primero): la cuenta de recuentos se corta en el evento REABIERTO."""
        b = bodega
        sku = b.hueco('C', teorico=30)
        b.programar(b.admin, 'C')
        sid = b.raiz_de(sku).id
        b.get(b.op_a, '/api/mobile/tarea-actual')
        for pos in (1, 2, 3):
            b.siesa.poner(sku, existencia=30, pos=pos)
            st, r = b.confirmar(b.op_a, sid, 30 - pos)
        assert r['resultado'] == 'BLOQUEADO'
        st, r = b.post(b.supervisor, f'/api/conteo/{sid}/reabrir', {})
        assert st == 200
        b.get(b.op_a, '/api/mobile/tarea-actual')
        b.siesa.poner(sku, existencia=30, pos=4)
        st, r = b.confirmar(b.op_a, sid, 26)
        assert r['resultado'] == 'RECONTAR', r


# ═════════════════════════════════════════════════════════════════════════════
# 6 · «No lo encontré»
# ═════════════════════════════════════════════════════════════════════════════

def _no_lo_encontre(b, operario, sid):
    return b.post(operario, '/api/mobile/reportar-problema',
                  {'tarea_id': sid, 'tipo': 'CONTEO', 'motivo': 'NO_ENCONTRADO'})


class TestNoLoEncontre:

    def test_bloquea_el_lider_reabre_respetando_el_doble_ciego(self, bodega):
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('A', teorico=50)
        b.programar(b.admin, 'A')
        raiz = b.raiz_de(sku).id

        # CC1 de Ana fuera de tolerancia → CC2 a Beto, que no lo encuentra.
        b.contar(b.op_a, raiz, 40)
        st, r = b.confirmar(b.op_a, raiz, 40)
        cc2 = r['segundo_conteo_id']
        st, t = b.get(b.op_b, '/api/mobile/tarea-actual')
        assert t['id'] == cc2
        # «Otro problema» sin contar qué pasó no se acepta.
        st, r = b.post(b.op_b, '/api/mobile/reportar-problema',
                       {'tarea_id': cc2, 'tipo': 'CONTEO', 'motivo': 'OTRO'})
        assert st == 409, r
        # Solo el dueño lo bloquea.
        st, r = _no_lo_encontre(b, b.op_c, cc2)
        assert st == 403, r
        st, r = _no_lo_encontre(b, b.op_b, cc2)
        assert st == 200 and r['motivo'] == 'NO_ENCONTRADO', r
        assert 'no se ajusta nada' in r['mensaje']
        s = b.sesion(cc2)
        assert (s.estado, s.motivo_bloqueo, s.cantidad_fisica) == (EstadoConteo.BLOQUEADO, 'NO_ENCONTRADO', None)
        assert b.sesion(raiz).estado == EstadoConteo.SEGUNDO_CONTEO
        assert b.jobs_ajuste(raiz) == []
        # No es un cero: nada lo cierra por la puerta de contar.
        st, r = b.confirmar(b.op_b, cc2, 0, cero=True)
        assert st == 400, r

        # El tablero del líder lo muestra, el primero de la cola.
        st, t = b.get(b.supervisor, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert st == 200
        bloq = t['decisiones']['bloqueados']
        assert bloq['total'] == 1 and bloq['filas'][0]['id'] == cc2, bloq
        assert bloq['filas'][0]['nivel'] == 'CC2' and bloq['filas'][0]['motivo_texto'] == 'No lo encontró'
        assert t['resumen']['por_bloque']['bloqueados'] == 1
        assert t['orden'][0] == 'bloqueados'
        # Los números no están en la fila: el líder puede terminar contando esta cadena.
        assert_ciego(bloq, 50, 40)

        # Dárselo a Ana, que contó el CC1, no se puede.
        st, r = b.post(b.supervisor, f'/api/conteo/{cc2}/reabrir', {'operario_id': b.op_a.id})
        assert st == 400 and 'Doble ciego' in r['error'], r
        assert b.sesion(cc2).estado == EstadoConteo.BLOQUEADO
        # Reabrir sin dueño: vuelve al pool…
        st, r = b.post(b.supervisor, f'/api/conteo/{cc2}/reabrir', {})
        assert st == 200, r
        s = b.sesion(cc2)
        assert (s.estado, s.operario_id) == (EstadoConteo.PENDIENTE, None)
        # …y el pool no se lo da a Ana.
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t.get('sin_tareas'), t
        st, r = b.post(b.supervisor, '/api/conteo/asignar-lote',
                       {'operario_id': b.op_a.id, 'almacen_id': b.almacen.id})
        assert st == 200 and r['asignadas'] == 0, r
        # Carolina sí lo toma, y la cadena sigue.
        st, t = b.get(b.op_c, '/api/mobile/tarea-actual')
        assert t['id'] == cc2, t
        st, r = b.confirmar(b.op_c, cc2, 50)
        assert st == 200 and r['resultado'] == 'MATCH', r
        assert b.sesion(raiz).estado == EstadoConteo.MATCH

    def test_el_lider_cancela_toda_la_cadena_y_el_hueco_se_libera(self, bodega):
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('C', teorico=20)
        b.programar(b.admin, 'C')
        raiz = b.raiz_de(sku).id
        b.get(b.op_a, '/api/mobile/tarea-actual')
        st, r = _no_lo_encontre(b, b.op_a, raiz)
        assert st == 200, r
        # Bloqueado traba el hueco: el generador no abre otra al lado.
        r = b.programar(b.admin, 'C')
        assert r['tareas_creadas'] == 0 and r['omitidos_por_pendiente'] == 1, r
        # Sin motivo no se cancela; un operario tampoco.
        st, r = b.put(b.supervisor, f'/api/conteo/{raiz}/cancelar', {})
        assert st == 400, r
        st, r = b.put(b.op_b, f'/api/conteo/{raiz}/cancelar', {'motivo': 'x'})
        assert st == 403, r
        st, r = b.put(b.jefe, f'/api/conteo/{raiz}/cancelar', {'motivo': 'descontinuado'})
        assert st == 200, r
        s = b.sesion(raiz)
        assert s.estado == EstadoConteo.CANCELADO and 'descontinuado' in s.motivo_edicion
        st, t = b.get(b.supervisor, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert t['decisiones']['bloqueados']['total'] == 0
        # Cancelar otra vez: la cadena ya cerró.
        st, r = b.put(b.jefe, f'/api/conteo/{raiz}/cancelar', {'motivo': 'otra vez'})
        assert st == 409, r
        # El hueco vuelve a estar libre para el plan.
        r = b.programar(b.admin, 'C')
        assert r['tareas_creadas'] == 1, r
        assert b.raiz_de(sku).id != raiz


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Cancelar un CC2 cancela la cadena y libera el hueco
# ═════════════════════════════════════════════════════════════════════════════

class TestCancelarUnSegundoConteo:

    def test_cancelar_el_cc2_cancela_la_cadena(self, bodega):
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('A', teorico=50)
        b.programar(b.admin, 'A')
        raiz = b.raiz_de(sku).id
        b.contar(b.op_a, raiz, 40)
        st, r = b.confirmar(b.op_a, raiz, 40)
        cc2 = r['segundo_conteo_id']
        # Beto ya lo abrió.
        st, t = b.get(b.op_b, '/api/mobile/tarea-actual')
        assert t['id'] == cc2
        r = b.programar(b.admin, 'A')
        assert r['tareas_creadas'] == 0, 'con la cadena viva no se abre otra'

        st, r = b.put(b.supervisor, f'/api/conteo/{cc2}/cancelar', {'motivo': 'se contó mal el CC1'})
        assert st == 200, r
        assert (b.sesion(cc2).estado, b.sesion(raiz).estado) == (EstadoConteo.CANCELADO,
                                                                  EstadoConteo.CANCELADO)
        # Beto ya no lo tiene, ni puede cerrarlo.
        st, t = b.get(b.op_b, '/api/mobile/tarea-actual')
        assert t.get('sin_tareas'), t
        st, r = b.confirmar(b.op_b, cc2, 50)
        assert st == 400, r
        assert b.jobs_ajuste(raiz) == []
        # Y el hueco se puede volver a programar.
        r = b.programar(b.admin, 'A')
        assert r['tareas_creadas'] == 1, r
        nueva = b.raiz_de(sku)
        assert nueva.id not in (raiz, cc2) and nueva.estado == EstadoConteo.PENDIENTE

    def test_cancelar_el_cc3_tambien(self, bodega):
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('A', teorico=50)
        b.programar(b.admin, 'A')
        raiz, cc2, cc3 = _cadena_hasta_definitivo(b, sku)
        st, r = b.put(b.supervisor, f'/api/conteo/{cc3}/cancelar', {'motivo': 'no aplica'})
        assert st == 200, r
        assert b.sesion(raiz).estado == EstadoConteo.CANCELADO
        assert b.sesion(cc3).estado == EstadoConteo.CANCELADO
        st, cola = b.get(b.supervisor, f'/api/conteo/definitivos?almacen_id={b.almacen.id}')
        assert cola['total'] == 0
        assert b.programar(b.admin, 'A')['tareas_creadas'] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 8 · /editar: solo se corrige la raíz ya contada
# ═════════════════════════════════════════════════════════════════════════════

class TestEditar:

    def _editar(self, b, sid, cantidad, usuario=None):
        return b.put(usuario or b.admin, f'/api/conteo/{sid}/editar',
                     {'cantidad_fisica': cantidad, 'motivo_edicion': 'error de digitación'})

    def test_que_se_corrige_y_que_no(self, bodega):
        from app.models.conteo import EstadoConteo
        b = bodega
        pendiente, cancelado, con_cc2, descuadre = (b.hueco('A', teorico=50) for _ in range(4))
        b.programar(b.admin, 'A')

        # PENDIENTE: nadie contó — no se «corrige» a MATCH.
        sid = b.raiz_de(pendiente).id
        st, r = self._editar(b, sid, 50)
        assert st == 409 and 'PENDIENTE' in r['error'], r
        assert b.sesion(sid).estado == EstadoConteo.PENDIENTE

        # CANCELADO: no resucita.
        sid = b.raiz_de(cancelado).id
        st, _ = b.put(b.supervisor, f'/api/conteo/{sid}/cancelar', {'motivo': 'no aplica'})
        st, r = self._editar(b, sid, 50)
        assert st == 409 and 'CANCELADO' in r['error'], r
        assert b.sesion(sid).estado == EstadoConteo.CANCELADO

        # Un CC2 (y su raíz con el CC2 vivo colgando): 409 con el porqué.
        raiz = b.raiz_de(con_cc2).id
        b.contar(b.op_a, raiz, 40)
        st, r = b.confirmar(b.op_a, raiz, 40)
        cc2 = r['segundo_conteo_id']
        st, r = self._editar(b, cc2, 50)
        assert st == 409 and 'CC2' in r['error'], r
        st, r = self._editar(b, raiz, 50)
        assert st == 409 and 'en curso' in r['error'], r
        assert b.sesion(raiz).estado == EstadoConteo.SEGUNDO_CONTEO

        # Un jefe no edita (LEAD).
        raiz_d, cc2_d, cc3_d = _cadena_hasta_definitivo(b, descuadre)
        b.contar(b.supervisor, cc3_d, 48)
        assert b.sesion(raiz_d).estado == EstadoConteo.DESCUADRE
        st, r = self._editar(b, raiz_d, 50, usuario=b.jefe)
        assert st == 403, r
        # El CC3 que resolvió tampoco: la cantidad que decide es la de la raíz.
        st, r = self._editar(b, cc3_d, 50)
        assert st == 409 and 'CC3' in r['error'], r
        # Sin motivo, no.
        st, r = b.put(b.admin, f'/api/conteo/{raiz_d}/editar', {'cantidad_fisica': 50})
        assert st == 400, r

        # Raíz en DESCUADRE: se corrige, y cuadrando pasa a MATCH.
        st, r = self._editar(b, raiz_d, 49)
        assert st == 200, r
        s = b.sesion(raiz_d)
        assert (s.estado, s.cantidad_fisica, s.diferencia) == (EstadoConteo.DESCUADRE, 49, -1)
        assert s.editado_por == b.admin.id and s.motivo_edicion == 'error de digitación'
        st, r = self._editar(b, raiz_d, 50)
        assert st == 200 and 'estado → MATCH' in r['cambios'], r
        assert b.sesion(raiz_d).estado == EstadoConteo.MATCH
        # Y un MATCH que se corrige a otra cifra vuelve a DESCUADRE.
        st, r = self._editar(b, raiz_d, 52)
        assert st == 200 and b.sesion(raiz_d).estado == EstadoConteo.DESCUADRE

        # En vuelo a Siesa: no.
        st, r = b.put(b.supervisor, f'/api/conteo/{raiz_d}/ajustar')
        assert st == 202, r
        st, r = self._editar(b, raiz_d, 50)
        assert st == 409, r

    def test_reasignar_un_conteo_en_curso_no_le_hereda_el_parcial_al_otro(self, bodega):
        """`/editar` con `operario_id` sobre un conteo EN_PROCESO se lo pasaba
        a otro operario CON lo que el primero llevaba contado y su foto de
        apertura: el segundo seguía, a ciegas, desde el parcial ajeno — la
        clase que `devolver_al_pool` existe para cerrar — y lo del primero se
        perdía sin rastro."""
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('C', teorico=10)
        b.programar(b.admin, 'C')
        sid = b.raiz_de(sku).id
        st, t = b.get(b.op_a, '/api/mobile/tarea-actual')
        assert t['id'] == sid
        b.post(b.op_a, '/api/mobile/conteo/total', {'tarea_id': sid, 'total_acumulado': 7})
        st, r = b.put(b.admin, f'/api/conteo/{sid}/editar',
                      {'operario_id': b.op_b.id, 'motivo_edicion': 'Ana salió a almorzar'})
        assert st == 200, r
        s = b.sesion(sid)
        assert (s.estado, s.operario_id, s.cantidad_fisica, s.foto_inicio_at) == (
            EstadoConteo.PENDIENTE, b.op_b.id, None, None), 'Beto arranca desde cero'
        rastro = s.lista_conteos_descartados()
        assert [(e['cantidad_fisica'], e['operario_id']) for e in rastro] == [(7, b.op_a.id)], rastro
        # Beto lo recibe desde cero, y con su propia foto de apertura.
        st, t = b.get(b.op_b, '/api/mobile/tarea-actual')
        assert (t['id'], t['cantidad_escaneada']) == (sid, 0), t
        assert b.sesion(sid).foto_inicio_at is not None
        # Ana ya no puede seguir escribiéndole.
        st, r = b.post(b.op_a, '/api/mobile/conteo/total', {'tarea_id': sid, 'total_acumulado': 9})
        assert st == 400, r
        # Y las estadísticas no lo cuentan como un recuento.
        st, e = b.get(b.supervisor, f'/api/conteo/estadisticas?almacen_id={b.almacen.id}')
        assert e['volumen']['recuentos']['numerador'] == 0
        assert e['volumen']['recuentos_propios'] == 0

    def test_reasignar_un_conteo_ya_contado_no_reescribe_quien_conto(self, bodega):
        """Cambiarle el dueño a un CC1 ya contado reescribía quién contó, y con
        eso el doble ciego: el CC1 de Ana pasaba a ser «de Caro», y el CC2 se le
        podía dar a Ana — la misma persona contaba las dos veces."""
        from app.models.conteo import EstadoConteo
        b = bodega
        sku = b.hueco('A', teorico=50)
        b.programar(b.admin, 'A')
        raiz = b.raiz_de(sku).id
        b.contar(b.op_a, raiz, 40)
        st, r = b.confirmar(b.op_a, raiz, 40)
        cc2 = r['segundo_conteo_id']
        st, r = b.put(b.admin, f'/api/conteo/{raiz}/editar',
                      {'operario_id': b.op_c.id, 'motivo_edicion': 'lo contó Caro'})
        assert st == 409, r
        assert b.sesion(raiz).operario_id == b.op_a.id
        st, r = b.put(b.admin, f'/api/conteo/{cc2}/editar',
                      {'operario_id': b.op_a.id, 'motivo_edicion': 'que lo haga Ana'})
        assert st == 400 and 'Doble ciego' in r['error'], r
        assert b.sesion(cc2).operario_id == b.op_b.id
        # Reasignar el CC2 que nadie empezó a otra persona sí se puede.
        st, r = b.put(b.admin, f'/api/conteo/{cc2}/editar',
                      {'operario_id': b.op_c.id, 'motivo_edicion': 'Beto no vino'})
        assert st == 200, r
        assert (b.sesion(cc2).operario_id, b.sesion(cc2).estado) == (b.op_c.id, EstadoConteo.PENDIENTE)
        # Un MATCH tampoco cambia de dueño.
        sku2 = b.hueco('C', teorico=10)
        b.programar(b.admin, 'C')
        s2 = b.raiz_de(sku2).id
        b.contar(b.op_a, s2, 10)
        st, r = b.put(b.admin, f'/api/conteo/{s2}/editar',
                      {'operario_id': b.op_b.id, 'motivo_edicion': 'x'})
        assert st == 409, r
        assert b.sesion(s2).operario_id == b.op_a.id

    def test_la_pantalla_sabe_por_que_no_se_edita(self, bodega):
        """La lista del supervisor trae el motivo por fila: la pantalla no
        promete un campo editable que el servidor va a rechazar."""
        b = bodega
        sku = b.hueco('A', teorico=50)
        b.programar(b.admin, 'A')
        sid = b.raiz_de(sku).id
        st, lista = b.get(b.supervisor, f'/api/conteo/?almacen_id={b.almacen.id}')
        fila = next(x for x in lista['sesiones'] if x['id'] == sid)
        motivo_pantalla = fila['no_se_corrige_cantidad']
        st, r = b.put(b.admin, f'/api/conteo/{sid}/editar',
                      {'cantidad_fisica': 50, 'motivo_edicion': 'x'})
        assert st == 409
        assert motivo_pantalla == r['error'], (fila, r)


# ═════════════════════════════════════════════════════════════════════════════
# El día completo — lo que miran el tablero, las estadísticas y la auditoría
# ═════════════════════════════════════════════════════════════════════════════

def _un_dia(b, monkeypatch):
    """Doce cadenas de un día, todas por HTTP (menos la auditoría por faltante,
    que nace de un picking, y la DLQ, que es un proceso). Devuelve las raíces."""
    from app.models.producto import Producto
    from app.models.siesa_job import SiesaJob
    from app.models.ubicacion import Ubicacion
    from app.services.connekta_gateway import ConnektaGateway
    from app.services.conteo_service import ConteoService
    from app.services.siesa_job_service import _ejecutar_job

    monkeypatch.setattr(ConnektaGateway, 'enviar_ajuste_inventario',
                        lambda self, **kw: {'codigo': 0})
    h = {
        'ok1': b.hueco('C', teorico=20),
        'ok1_mov': b.hueco('C', teorico=30),
        'tol': b.hueco('C', teorico=100),
        'err_conf': b.hueco('A', teorico=50),
        'err_cc3': b.hueco('A', teorico=50),
        'aprobable': b.hueco('A', teorico=50),
        'ok_cc2': b.hueco('A', teorico=50),
        'ajuste_bloqueado': b.hueco('A', teorico=50, pos=2, salida_sin_conf=5),
        'no_encontrado': b.hueco('B', teorico=10),
        'cancelada': b.hueco('B', teorico=10),
        'con_novedad': b.hueco('C', teorico=10),
        'auditoria': b.hueco('B', teorico=10),
    }
    auditoria = h.pop('auditoria')
    st, r = b.post(b.admin, '/api/conteo/abc/generar-todas', {'almacen_id': b.almacen.id})
    assert st == 201 and r['total_tareas_creadas'] == 12, r
    ids = {k: b.raiz_de(sku).id for k, sku in h.items()}

    # La auditoría por faltante: el picker no encontró el producto. Ya había un
    # conteo del plan pendiente sobre ese hueco: la auditoría lo CONVIERTE.
    p = Producto.query.filter_by(codigo=auditoria).one()
    u = Ubicacion.query.filter_by(codigo=f'E2E-UB-{int(auditoria[3:]):03d}').one()
    aud = ConteoService.generar_auditoria_por_excepcion(None, u.id, p.id, b.almacen.id)
    b.db.session.commit()
    ids['auditoria'] = aud.id
    assert b.raiz_de(auditoria).id == aud.id and aud.tipo == 'EXCEPCION_PICKING'

    # OK_CC1
    assert b.contar(b.op_a, ids['ok1'], 20)['resultado'] == 'MATCH'
    # OK_CC1 con un recuento por venta durante el conteo
    b.abrir(b.op_a, ids['ok1_mov'])
    b.siesa.poner(h['ok1_mov'], existencia=30, pos=1)
    assert b.confirmar(b.op_a, ids['ok1_mov'], 29)[1]['resultado'] == 'RECONTAR'
    assert b.confirmar(b.op_a, ids['ok1_mov'], 29)[1]['resultado'] == 'MATCH'
    # AJUSTE_EN_TOLERANCIA, sale solo y la DLQ lo lleva a Siesa
    r = b.contar(b.op_a, ids['tol'], 98)
    assert r['resultado'] == 'DENTRO_TOLERANCIA', r
    _ejecutar_job(b.jobs_ajuste(ids['tol'])[0])
    assert b.sesion(ids['tol']).estado == 'AJUSTADO'

    def cc1_fuera(clave, fisico):
        r = b.contar(b.op_a, ids[clave], fisico)
        assert r['resultado'] == 'RECONTAR_TU', r
        st, r = b.confirmar(b.op_a, ids[clave], fisico)
        assert r['resultado'] == 'SEGUNDO_CONTEO', r
        return r['segundo_conteo_id']

    # ERROR_CONFIRMADO, ajuste automático (CC1 == CC2)
    cc2 = cc1_fuera('err_conf', 46)
    assert b.contar(b.op_b, cc2, 46)['resultado'] == 'DESCUADRE'
    assert b.sesion(ids['err_conf']).estado == 'AJUSTANDO'
    # ERROR_CC3 aprobado por el supervisor
    cc2 = cc1_fuera('err_cc3', 45)
    cc3 = b.contar(b.op_b, cc2, 47)['tercer_conteo_id']
    assert b.contar(b.supervisor, cc3, 48)['resultado'] == 'DESCUADRE'
    assert b.put(b.supervisor, f'/api/conteo/{ids["err_cc3"]}/ajustar')[0] == 202
    # ERROR_CC3 esperando la firma
    cc2 = cc1_fuera('aprobable', 45)
    cc3 = b.contar(b.op_b, cc2, 47)['tercer_conteo_id']
    assert b.contar(b.supervisor, cc3, 48)['resultado'] == 'DESCUADRE'
    # OK_CC2
    cc2 = cc1_fuera('ok_cc2', 45)
    assert b.contar(b.op_b, cc2, 50)['resultado'] == 'MATCH'
    # ERROR_CONFIRMADO que no puede ajustar: salidas sin confirmar que no son POS
    cc2 = cc1_fuera('ajuste_bloqueado', 46)
    r = b.contar(b.op_b, cc2, 46)
    assert r['resultado'] == 'DESCUADRE' and b.sesion(ids['ajuste_bloqueado']).estado == 'DESCUADRE'
    assert b.jobs_ajuste(ids['ajuste_bloqueado']) == []
    # «No lo encontré»
    b.abrir(b.op_a, ids['no_encontrado'])
    assert _no_lo_encontre(b, b.op_a, ids['no_encontrado'])[0] == 200
    # Cancelada antes de contarse
    assert b.put(b.supervisor, f'/api/conteo/{ids["cancelada"]}/cancelar',
                 {'motivo': 'producto descontinuado'})[0] == 200
    # En curso, con mercancía sin código anotada para el líder
    b.abrir(b.op_c, ids['con_novedad'])
    st, r = b.post(b.op_c, '/api/mobile/conteo/sin-codigo',
                   {'tarea_id': ids['con_novedad'], 'descripcion': '3 cajas sin etiqueta al fondo'})
    assert st == 200, r
    ids['novedad'] = r['novedad_id']
    assert SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO').count() == 3
    return ids


# ═════════════════════════════════════════════════════════════════════════════
# 9 · El tablero del líder
# ═════════════════════════════════════════════════════════════════════════════

#: Cada permiso del tablero y cómo preguntárselo al endpoint que lo exige, con
#: un id que no existe: si el rol pasa el permiso el endpoint sigue y contesta
#: otra cosa (404/400/200); si no, 403. Nada se modifica.
SONDAS = {
    'reabrir_cancelar_bloqueado': ('post', '/api/conteo/999999/reabrir', {}),
    'resolver_novedad': ('post', '/api/conteo/novedades/999999/resolver', {'nota': 'x'}),
    'aprobar_ajuste': ('put', '/api/conteo/999999/ajustar', {}),
    'recontar': ('post', '/api/conteo/manual', {'almacen_id': None, 'producto_codigo': 'NO-EXISTE'}),
    'cancelar_conteo': ('put', '/api/conteo/999999/cancelar', {'motivo': 'x'}),
    'reintentar_descartar_fallos': ('get', '/api/conteo/descartar-fallos/preview', None),
    'cancelar_rezago': ('get', '/api/conteo/abc/limpiar-pendientes/preview?almacen_id={alm}', None),
}


class TestTableroDelLider:

    def test_cada_bloque_refleja_el_dia(self, bodega, monkeypatch):
        from app.models.siesa_job import EstadoSiesaJob
        b = bodega
        ids = _un_dia(b, monkeypatch)
        # Siesa rechazó el ajuste de la verdad de bodega.
        job = b.jobs_ajuste(ids['err_conf'])[0]
        job.estado = EstadoSiesaJob.FALLIDO
        job.error_ultimo = 'Siesa: el tipo de documento no está autorizado'
        b.db.session.commit()

        st, t = b.get(b.supervisor, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert st == 200, t
        d = t['decisiones']
        assert [f['id'] for f in d['bloqueados']['filas']] == [ids['no_encontrado']]
        assert d['bloqueados']['por_motivo'] == {'NO_ENCONTRADO': 1}
        assert [f['id'] for f in d['novedades']['filas']] == [ids['novedad']]
        aj = d['ajustes']
        assert [f['id'] for f in aj['aprobables']['filas']] == [ids['aprobable']]
        assert aj['aprobables']['valor_total'] == 2000.0 and aj['aprobables']['sin_costo'] == 0
        assert [f['id'] for f in aj['bloqueados']['filas']] == [ids['ajuste_bloqueado']]
        assert aj['bloqueados']['por_motivo'] == {'SALIDAS_NO_POS': 1}
        assert aj['bloqueados']['filas'][0]['accion']['tipo'] == 'RECONTAR'
        assert aj['total_descuadres'] == 2, 'solo raíces: los CC2/CC3 resueltos no son decisiones'
        aud = d['auditorias']
        assert [f['id'] for f in aud['filas']] == [ids['auditoria']]
        assert aud['esperan_al_lider'] == 0 and aud['filas'][0]['accion']['tipo'] == 'EN_COLA'
        assert [f['id'] for f in d['rechazados_siesa']['filas']] == [ids['err_conf']]
        assert d['rechazados_siesa']['total_sistema'] == 1
        assert t['resumen'] == {'decisiones_pendientes': 5, 'por_bloque': {
            'bloqueados': 1, 'novedades': 1, 'ajustes': 2, 'auditorias': 0, 'rechazados_siesa': 1}}
        hoy = t['hoy']
        assert hoy['cerrados'] == 8, hoy
        assert hoy['pendientes_vivas'] == 2, hoy       # la auditoría y el que se está contando
        # El KPI del dashboard es el mismo número que el bloque.
        from app.services.tablero_lider_conteo import contar_auditorias_urgentes
        assert contar_auditorias_urgentes(b.almacen.id) == aud['total']

        # Las acciones del tablero, por HTTP, lo vacían.
        st, r = b.post(b.supervisor, f'/api/conteo/novedades/{ids["novedad"]}/resolver',
                       {'nota': 'eran del producto E2E011, se sumaron'})
        assert st == 200, r
        st, r = b.post(b.supervisor, '/api/conteo/reintentar-fallos')
        assert st == 200 and r['reencolados'] == 1, r
        st, r = b.put(b.supervisor, f'/api/conteo/{ids["ajuste_bloqueado"]}/cancelar',
                      {'motivo': 'recontar cuando confirmen la remisión'})
        assert st == 200, r
        st, r = b.put(b.supervisor, f'/api/conteo/{ids["aprobable"]}/ajustar')
        assert st == 202, r
        st, r = b.put(b.supervisor, f'/api/conteo/{ids["no_encontrado"]}/cancelar',
                      {'motivo': 'no está en la bodega'})
        assert st == 200, r
        st, t = b.get(b.supervisor, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert t['resumen']['decisiones_pendientes'] == 0, t['resumen']

    @pytest.mark.parametrize('tope_jefe', ['0', '5000'])
    def test_ningun_boton_promete_un_403(self, bodega, monkeypatch, tope_jefe):
        b = bodega
        monkeypatch.setenv('CONTEO_TOPE_APROBACION_JEFE', tope_jefe)
        for usuario in (b.jefe, b.supervisor, b.admin):
            st, t = b.get(usuario, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
            assert st == 200, t
            assert set(t['permisos']) == set(SONDAS), 'un permiso nuevo necesita su sonda'
            for permiso, (metodo, url, body) in SONDAS.items():
                url = url.format(alm=b.almacen.id)
                if body and 'almacen_id' in body:
                    body = {**body, 'almacen_id': b.almacen.id}
                st, r = getattr(b, metodo)(usuario, url, *([body] if body is not None else []))
                assert (st != 403) == t['permisos'][permiso], (usuario.rol, permiso, st, r)
        # El operario ni siquiera ve el tablero.
        st, _ = b.get(b.op_a, f'/api/conteo/lider/tablero?almacen_id={b.almacen.id}')
        assert st == 403


# ═════════════════════════════════════════════════════════════════════════════
# 10 · Las estadísticas cuadran con lo que pasó
# ═════════════════════════════════════════════════════════════════════════════

class TestEstadisticas:

    def test_cuadran_con_el_dia(self, bodega, monkeypatch):
        b = bodega
        ids = _un_dia(b, monkeypatch)
        st, r = b.get(b.op_a, '/api/conteo/estadisticas')
        assert st == 403
        st, e = b.get(b.supervisor, f'/api/conteo/estadisticas?almacen_id={b.almacen.id}')
        assert st == 200, e
        v = e['volumen']
        assert v['iniciadas'] == 12 and v['cerradas'] == 8, v
        assert v['por_veredicto'] == {
            'OK_CC1': 2, 'OK_CC2': 1, 'OK_CC3': 0, 'ERROR_CONFIRMADO': 2, 'ERROR_CC3': 2,
            'ERROR_AUDITORIA': 0, 'AJUSTE_EN_TOLERANCIA': 1}, v['por_veredicto']
        assert v['sin_veredicto'] == {'cancelada': 1, 'en_curso': 1, 'no_encontrado': 1,
                                      'pendiente': 1}, v['sin_veredicto']
        assert (v['a_cc2']['numerador'], v['a_cc2']['denominador']) == (5, 8)
        assert (v['a_cc3']['numerador'], v['a_cc3']['denominador']) == (2, 8)
        # Recuentos: por venta durante el conteo (1) vs propios por tolerancia (5).
        assert v['recuentos']['numerador'] == 1, v['recuentos']
        assert v['recuentos_propios'] == 5
        assert v['por_tipo']['EXCEPCION_PICKING'] == {'iniciadas': 1, 'cerradas': 0}

        a = e['ajustes']
        assert a['cantidad'] == 3, a
        assert (a['automaticos'], a['por_tolerancia'], a['aprobados_por_supervisor']) == (2, 1, 1)
        assert (a['unidades_ent'], a['unidades_sal']) == (0, 2 + 4 + 2)
        assert a['valor']['sal'] == 8000.0
        assert a['bloqueados_hoy']['por_motivo'] == {'SALIDAS_NO_POS': 1}
        assert a['bloqueados_hoy']['descuadres_aprobables'] == 1

        # Exactitud: 3 OK de 8 (exacta); con tolerancia el primer conteo de
        # ok1, ok1_mov y tol quedó dentro.
        exact = e['exactitud']
        oks = sum(g['numerador'] for cl in exact['por_clase'].values() for g in cl.values())
        tot = sum(g['denominador'] for cl in exact['por_clase'].values() for g in cl.values())
        assert (oks, tot) == (3, 8), exact
        # Por persona: solo volumen.
        filas = {f.get('operario') or f.get('nombre'): f for f in e['por_operario']['filas']}
        assert filas, e['por_operario']

        # El mismo reporte por clase no inventa nada.
        st, ea = b.get(b.supervisor, f'/api/conteo/estadisticas?almacen_id={b.almacen.id}&clase=A')
        assert ea['volumen']['cerradas'] == 5 and ea['volumen']['recuentos']['numerador'] == 0
        st, r = b.get(b.supervisor, '/api/conteo/estadisticas?clase=Z')
        assert st == 400
        st, r = b.get(b.supervisor, '/api/conteo/estadisticas?desde=ayer')
        assert st == 400


# ═════════════════════════════════════════════════════════════════════════════
# 11 · La auditoría no ve nada roto en un día sano
# ═════════════════════════════════════════════════════════════════════════════

class TestAuditoria:

    def test_ningun_bloqueante_sobre_el_dia(self, bodega, monkeypatch):
        b = bodega
        _un_dia(b, monkeypatch)
        st, r = b.get(b.op_a, '/api/auditoria/flujo?flujo=conteo')
        assert st == 403
        st, r = b.get(b.supervisor, '/api/auditoria/flujo?flujo=conteo')
        assert st == 200, r
        assert not r['errores'], r['errores']
        rotos = [(x['codigo'], x['total'], x.get('muestra') or x.get('hallazgos'))
                 for x in r['resultados'] if x['severidad'] == 'BLOQUEA' and x['total']]
        assert not rotos, rotos
        assert len(r['resultados']) >= 9, 'la auditoría de conteo dejó de correr invariantes'


# ═════════════════════════════════════════════════════════════════════════════
# Defecto encontrado: la respuesta al que cuenta traía el motivo del ajuste
# ═════════════════════════════════════════════════════════════════════════════
#
# La clase: **lo que se le devuelve a quien cuenta explica el ajuste**, y el
# ajuste se explica con las cifras de Siesa. El 2026-09-23 se había cerrado la
# instancia del RECONTAR; quedaban la de tolerancia (`ajuste_bloqueado`,
# `detalle_ajuste`, `no_sale_solo`) y la del CC2 (lo mismo, y dentro del
# `mensaje`). La política vive en `ConteoService.respuesta_para_quien_cuenta`
# y el trinquete de abajo exige que toda llamada a `registrar_conteo` en
# `app/` pase su resultado por ella.

import ast  # noqa: E402
import pathlib  # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parents[2]


class TestLaRespuestaAlQueCuentaEsCiega:

    @pytest.mark.parametrize('puerta', ['mobile', 'registrar'])
    def test_segundo_conteo_con_ajuste_bloqueado(self, bodega, puerta):
        b = bodega
        sku = b.hueco('A', teorico=50, pos=2, salida_sin_conf=5)
        b.programar(b.admin, 'A')
        raiz = b.raiz_de(sku).id
        b.contar(b.op_a, raiz, 46)
        st, r = b.confirmar(b.op_a, raiz, 46)
        cc2 = r['segundo_conteo_id']
        st, _ = b.abrir(b.op_b, cc2)
        if puerta == 'mobile':
            st, r = b.confirmar(b.op_b, cc2, 46)
        else:
            st, r = b.post(b.op_b, f'/api/conteo/{cc2}/registrar', {'cantidad_fisica': 46})
        assert st == 200 and r['resultado'] == 'DESCUADRE', r
        # Lo que pasó adentro, igual: la verdad de bodega no ajusta (salidas no POS).
        assert b.sesion(raiz).estado == 'DESCUADRE' and b.jobs_ajuste(raiz) == []
        assert_ciego(r, 50, 52, 46, 4, 2, 5, 3)
        assert set(r) <= set(ConteoService_claves()), r

    def test_dentro_de_tolerancia_por_la_otra_puerta(self, bodega, monkeypatch):
        b = bodega
        monkeypatch.setenv('CONTEO_TOLERANCIA_TOPE_VALOR', '1000000')
        monkeypatch.setenv('CONTEO_TOPE_AUTOAJUSTE', '2500')
        sku = b.hueco('C', teorico=100)
        b.programar(b.admin, 'C')
        sid = b.raiz_de(sku).id
        b.abrir(b.op_a, sid)
        st, r = b.post(b.op_a, f'/api/conteo/{sid}/registrar', {'cantidad_fisica': 97})
        assert st == 200 and r['resultado'] == 'DENTRO_TOLERANCIA', r
        assert_ciego(r, 100, 97, 3, 3000, '3.000', 2500, '2.500')

    def test_la_supervision_si_recibe_el_motivo(self, bodega):
        """El Conteo Definitivo lo hace un supervisor, que es quien aprueba:
        necesita saber YA que el ajuste no va a poder salir."""
        b = bodega
        sku = b.hueco('A', teorico=50, pos=2, salida_sin_conf=5)
        b.programar(b.admin, 'A')
        raiz, _, cc3 = _cadena_hasta_definitivo(b, sku, 45, 47)
        r = b.contar(b.supervisor, cc3, 48)
        assert r['resultado'] == 'DESCUADRE' and r['raiz_id'] == raiz, r
        assert 'salidas sin confirmar' in (r['ajuste_bloqueado'] or ''), r

    # ── el trinquete ─────────────────────────────────────────────────────────

    @staticmethod
    def _llamadores(fuentes):
        """`{archivo::funcion: pasa_por_la_politica}` para cada función de
        `app/` que llama `registrar_conteo`."""
        out = {}
        for rel, src in fuentes.items():
            for nodo in ast.walk(ast.parse(src)):
                if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                llamadas = {getattr(c.func, 'attr', getattr(c.func, 'id', None))
                            for c in ast.walk(nodo) if isinstance(c, ast.Call)}
                if 'registrar_conteo' in llamadas:
                    out[f'{rel}::{nodo.name}'] = 'respuesta_para_quien_cuenta' in llamadas
        return out

    def test_toda_puerta_que_cierra_un_conteo_pasa_por_la_politica(self):
        fuentes = {str(f.relative_to(RAIZ)): f.read_text(encoding='utf-8')
                   for f in sorted((RAIZ / 'app').rglob('*.py'))}
        llamadores = self._llamadores(fuentes)
        assert len(llamadores) >= 2, f'el escáner dejó de ver las puertas: {llamadores}'
        sin_politica = [k for k, ok in llamadores.items() if not ok]
        assert not sin_politica, (
            f'Cierran un conteo y devuelven el resultado crudo: {sin_politica}. '
            'Pasalo por ConteoService.respuesta_para_quien_cuenta.')

    def test_el_escaner_ve_la_puerta_sin_politica(self):
        src = ('def confirmar(x):\n'
               '    return ConteoService.registrar_conteo(1, 2, x)\n'
               'def bien(x):\n'
               '    r = ConteoService.registrar_conteo(1, 2, x)\n'
               '    return ConteoService.respuesta_para_quien_cuenta(r, 2)\n')
        assert self._llamadores({'x.py': src}) == {'x.py::confirmar': False, 'x.py::bien': True}


def ConteoService_claves():
    from app.services.conteo_service import ConteoService
    return ConteoService.CLAVES_PARA_QUIEN_CUENTA
