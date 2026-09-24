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
        assert r['resultado'] == 'DENTRO_TOLERANCIA' and r['auto_encolado'] is True, r
        assert r['mensaje'] == 'Conteo registrado — gracias.'
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
        assert r['resultado'] == 'DENTRO_TOLERANCIA' and r['auto_encolado'] is False, r
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
    st, t = b.get(b.op_b, '/api/mobile/tarea-actual')
    assert t['id'] == cc2_id, t
    st, r = b.confirmar(b.op_b, cc2_id, cc2)
    assert st == 200 and r['resultado'] == 'TERCER_CONTEO', r
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
