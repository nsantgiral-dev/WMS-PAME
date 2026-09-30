"""
Inventario cíclico de punta a punta, **escenario por escenario**, hasta el
documento 142951 que saldría a Siesa.

`test_e2e_inventario_ciclico.py` recorre un día de conteo por HTTP y se
detiene en el job encolado. Acá cada escenario sigue hasta el final: se ejecuta
el job `AJUSTE_CONTEO` con el gateway REAL armando el payload (solo `_post`
está parcheado: captura lo que viajaría), y después de cada escenario —y de los
pasos intermedios que importan— corre **un solo chequeo de inventario**
(`Mundo.verificar_inventario`, pedido del dueño 2026-09-29: «que siempre el
inventario esté bien»):

1. ninguna ubicación con cantidad < 0 ni reservado > cantidad;
2. cada cambio de cantidad del WMS dejó su `MovimientoInventario` con motivo
   (saldo por hueco = saldo inicial + movimientos del escenario);
3. cada ajuste que salió a Siesa movió exactamente `contado − teórico` de su
   foto, una sola vez, con el tipo/clase/concepto/motivo que le toca, y el WMS
   del SKU terminó en lo contado (+ el POS que Siesa no acumuló); un conteo que
   cuadró con Siesa también deja el WMS en lo contado (VAL-11);
4. el auditor de flujo de conteo (`auditoria.auditar('conteo')`) no tiene
   hallazgos BLOQUEA ni invariantes que revienten.

Siesa es de mentira en el formato crudo de `API_v2_Inventarios_InvFecha`
(`f400_cant_existencia_1`, `f400_cant_pos_1`, `f400_cant_salida_sin_conf_1`,
`f400_costo_prom_uni`), por SKU × bodega, con el maestro de ítems y la caída.
Las etapas que no son de conteo (picking, empaque, remisión) se avanzan con los
servicios reales (`tests/flujo/conductor_de_flujo.py`); lo que está antes del
WMS (el pedido de Siesa, un traslado ya despachado, una recepción sin entrada)
se siembra con la forma que dejan sus servicios.

Escenarios (letras del pedido del dueño): a · MATCH · b · CC1 = CC2 dentro y
fuera del tope · c · CC3 · d · POS · e · pedido comprometido · f · traslado en
tránsito y recepción sin entrada · g · un SKU en varios lugares · h · Siesa sin
fila y la entrada con costo · i · Siesa caído, foto vieja, zombi · j ·
asignación · k · omitir, editar, admin único.
"""
import json
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash


# ─────────────────────────────────────────────────────────────────────────────
# Siesa de mentira: InvFecha por SKU × bodega, maestro de ítems, 142951 capturado
# ─────────────────────────────────────────────────────────────────────────────

class SiesaE2E:
    COSTO = 1000.0

    def __init__(self):
        self.filas = {}          # (sku, bodega) → fila cruda de InvFecha
        self.maestro = set()     # referencias en API_v2_Items
        self.caida = False
        self.posts = []          # (conector, nombre, payload)

    def poner(self, sku, existencia, *, bodega='NB1', pos=0, salida_sin_conf=None,
              costo=COSTO, comprometida=0):
        ssc = pos if salida_sin_conf is None else salida_sin_conf
        fila = {
            'f120_referencia': sku, 'f150_id': bodega,
            'f400_cant_existencia_1': float(existencia),
            'f400_cant_comprometida_1': float(comprometida),
            'f400_cant_salida_sin_conf_1': float(ssc),
            'f400_cant_pos_1': float(pos),
            'f400_id_lote': '', 'f400_id_ubicacion_aux': None,
        }
        if costo is not None:
            fila['f400_costo_prom_uni'] = float(costo)
        self.filas[(sku, bodega)] = fila
        self.maestro.add(sku)

    def sin_fila(self, sku, bodega='NB1'):
        """El ítem existe en el maestro pero no tiene fila en la bodega."""
        self.filas.pop((sku, bodega), None)
        self.maestro.add(sku)

    # ── lo que el gateway ve ──────────────────────────────────────────────
    def inv_fecha(self, codigo, bodega=None):
        if self.caida:
            raise ConnectionError('Siesa no responde')
        fila = self.filas.get((codigo, bodega))
        return {'codigo': 0, 'detalle': {'Table': [dict(fila)] if fila else []}}

    def item(self, referencia):
        if self.caida:
            raise ConnectionError('Siesa no responde')
        if referencia not in self.maestro:
            return None
        return {'codigo_siesa': referencia, 'nombre': referencia, 'tipo_inventario': None}

    def existencias(self, referencia):
        if self.caida:
            raise ConnectionError('Siesa no responde')
        return {'detalle': {'Table': [
            {'f120_referencia': sku, 'f150_id': f'{b:<5}',
             'f400_cant_existencia_1': f['f400_cant_existencia_1'],
             'f400_costo_prom_uni': f.get('f400_costo_prom_uni', 0.0)}
            for (sku, b), f in self.filas.items() if sku == referencia]}}

    def post(self, conector, nombre, payload, **_kw):
        self.posts.append((conector, nombre, json.loads(json.dumps(payload))))
        return {'codigo': 0, 'mensaje': 'Transacción Exitosa', 'detalle': 'Importacion exitosa'}

    def posts_de(self, referencia):
        return [p for _c, _n, p in self.posts if p['Documentos'][0]['f350_notas'] == referencia]


@pytest.fixture
def siesa(monkeypatch):
    """Parcheado sobre la CLASE (CLAUDE.md, refactor del gateway); config en la
    instancia. `_post` es lo único del 142951 que no corre: el payload lo arma
    `enviar_ajuste_inventario` real."""
    from app.services.connekta_gateway import ConnektaGateway, connekta
    falsa = SiesaE2E()
    for metodo in ('get_inventario_fecha', 'buscar_item_por_referencia',
                   'get_existencias_por_referencia', '_post'):
        if metodo in vars(connekta):
            monkeypatch.delattr(connekta, metodo)
    monkeypatch.setattr(connekta, 'modo_simulacion', False)
    monkeypatch.setattr(connekta, 'tipo_docto_ajuste', 'ADI')
    monkeypatch.setattr(connekta, 'motivo_ajuste_entrada', '01')
    monkeypatch.setattr(connekta, 'motivo_ajuste_salida', '02')
    monkeypatch.setattr(connekta, 'concepto_ajustes', 603)
    monkeypatch.setattr(connekta, 'motivo_entrada_inventario', '01')
    monkeypatch.setattr(ConnektaGateway, 'get_inventario_fecha',
                        lambda self, codigo, bodega=None: falsa.inv_fecha(codigo, bodega))
    monkeypatch.setattr(ConnektaGateway, 'buscar_item_por_referencia',
                        lambda self, referencia: falsa.item(referencia))
    monkeypatch.setattr(ConnektaGateway, 'get_existencias_por_referencia',
                        lambda self, referencia: falsa.existencias(referencia))
    def _sin_red(self, *a, **k):
        raise ConnectionError('red bloqueada en el test')
    monkeypatch.setattr(ConnektaGateway, '_get', _sin_red)
    monkeypatch.setattr(ConnektaGateway, '_post',
                        lambda self, conector, nombre, payload, url=None, extra_params=None:
                        falsa.post(conector, nombre, payload))
    return falsa


@pytest.fixture(autouse=True)
def _politica_por_defecto(monkeypatch):
    from app.services.conteo_politica import VARIABLES_DE_ENTORNO
    for n in VARIABLES_DE_ENTORNO:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv('CONNEKTA_BODEGA', 'NB1')


# ─────────────────────────────────────────────────────────────────────────────
# El mundo: NB1 (CD) + NC1 (tienda), personas, productos, y el chequeo común
# ─────────────────────────────────────────────────────────────────────────────

class Mundo:
    def __init__(self, app, client, db, siesa, almacen):
        from app.models.almacen import Almacen
        self.app, self.client, self.db, self.siesa = app, client, db, siesa
        almacen.bodega_siesa_id, almacen.centro_op_siesa = 'NB1', '003'
        self.nb1 = almacen
        self.nc1 = Almacen(codigo='NC1-E2E', nombre='Neiva Centro', bodega_siesa_id='NC1',
                           centro_op_siesa='002', activo=True)
        db.session.add(self.nc1)
        db.session.commit()
        self._ubs, self._n = {}, 0
        self._base, self._mov_desde = {}, 0
        self.ejecutados = []            # (sesion_id, payload 142951)

    # ── personas ──────────────────────────────────────────────────────────
    def persona(self, nombre, rol, almacen=None, senal=True, **kw):
        from app.models.usuario import Usuario
        u = Usuario(nombre=nombre, email=f'{nombre}@e2e.test',
                    password_hash=generate_password_hash('x'), rol=rol,
                    almacen_id=(almacen or self.nb1).id, activo=True,
                    ultima_senal_at=datetime.utcnow() if senal else datetime.utcnow() - timedelta(hours=6),
                    **kw)
        self.db.session.add(u)
        self.db.session.commit()
        return u

    # ── lugares y productos ───────────────────────────────────────────────
    def ub(self, almacen, codigo, zona='GENERAL'):
        from app.models.ubicacion import Ubicacion
        clave = (almacen.id, codigo)
        if clave not in self._ubs:
            u = Ubicacion(codigo=codigo, almacen_id=almacen.id, tipo_zona=zona, stock_minimo=0,
                          stock_maximo=99999, secuencia_ruteo=len(self._ubs) + 1, activo=True)
            self.db.session.add(u)
            self.db.session.flush()
            self._ubs[clave] = u
        return self._ubs[clave]

    def producto(self, almacen=None, lugares=None, *, pos=0, salida_sin_conf=None,
                 costo=SiesaE2E.COSTO, en_siesa=True, existencia=None):
        """Un SKU con su stock en el WMS (`lugares`: código → cantidad) y su
        fila en Siesa. Por defecto el WMS está donde lo dejó la carga de Siesa:
        existencia = suma de los lugares; el teórico es existencia − POS."""
        from app.models.inventario import UbicacionProducto
        from app.models.producto import Producto
        almacen = almacen or self.nb1
        lugares = lugares if lugares is not None else {'SIESA-GENERAL': 50}
        self._n += 1
        sku = f'ESC{self._n:03d}'
        p = Producto(codigo=sku, nombre=f'Producto {sku}', codigo_siesa=sku,
                     codigo_barras=f'771{self._n:07d}', unidad_negocio_id='001',
                     factor_conversion=1, activo=True)
        self.db.session.add(p)
        self.db.session.flush()
        zonas = {'PIK': 'PICKING', 'RES': 'RESERVA'}
        for codigo, cant in lugares.items():
            u = self.ub(almacen, codigo, zonas.get(codigo[:3], 'GENERAL'))
            self.db.session.add(UbicacionProducto(ubicacion_id=u.id, producto_id=p.id,
                                                  cantidad=cant, reservado=0, bloqueado=0))
        self.db.session.commit()
        if en_siesa:
            self.siesa.poner(sku, sum(lugares.values()) if existencia is None else existencia,
                             bodega=almacen.bodega_siesa_id, pos=pos,
                             salida_sin_conf=salida_sin_conf, costo=costo)
        else:
            self.siesa.sin_fila(sku, almacen.bodega_siesa_id)
        self.foto_inicial()
        return p

    def simulando(self):
        """Las etapas que no son de conteo (cierre de caja, remisión) corren en
        modo simulación, como en `conductor_de_flujo`: acá no se prueba Siesa
        facturando, se prueba qué ve el conteo después."""
        import contextlib
        from app.services.connekta_gateway import connekta

        @contextlib.contextmanager
        def _ctx():
            connekta.modo_simulacion = True
            try:
                yield
            finally:
                connekta.modo_simulacion = False
        return _ctx()

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _h(self, u):
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}

    def get(self, u, url):
        r = self.client.get(url, headers=self._h(u))
        return r.status_code, r.get_json()

    def post(self, u, url, body=None):
        r = self.client.post(url, json=body or {}, headers=self._h(u))
        return r.status_code, r.get_json()

    def put(self, u, url, body=None):
        r = self.client.put(url, json=body or {}, headers=self._h(u))
        return r.status_code, r.get_json()

    # ── el conteo como lo hace la gente ───────────────────────────────────
    def manual(self, lider, p, almacen=None, operario=None):
        almacen = almacen or self.nb1
        body = {'almacen_id': almacen.id, 'producto_codigo': p.codigo}
        if operario is not None:
            body['operario_id'] = operario.id
        st, r = self.post(lider, '/api/conteo/manual', body)
        assert st == 201, r
        return self.raiz(p, almacen).id

    def raiz(self, p, almacen=None):
        from app.models.conteo import SesionConteo
        self.db.session.expire_all()
        return (SesionConteo.query
                .filter_by(producto_id=p.id, almacen_id=(almacen or self.nb1).id,
                           es_segundo_conteo=False)
                .order_by(SesionConteo.id.desc()).first())

    def s(self, sid):
        from app.models.conteo import SesionConteo
        self.db.session.expire_all()
        return self.db.session.get(SesionConteo, sid)

    def abrir(self, u, sid):
        st, r = self.get(u, f'/api/conteo/{sid}/tarea')
        assert st == 200, r
        return r

    def confirmar(self, u, sid, total):
        body = {'tarea_id': sid, 'tipo': 'CONTEO', 'items_escaneados': [], 'total_contado': total}
        if total == 0:
            body['cero_confirmado'] = True
        return self.post(u, '/api/mobile/confirmar', body)

    def contar(self, u, sid, total):
        self.abrir(u, sid)
        st, r = self.confirmar(u, sid, total)
        assert st == 200, r
        return r

    def cc1_cc2(self, sid, op1, op2, total1, total2=None):
        """CC1 fuera de tolerancia (y su recuento propio) → CC2 de otra persona."""
        r = self.contar(op1, sid, total1)
        assert r['resultado'] == 'RECONTAR_TU', r
        st, r = self.confirmar(op1, sid, total1)
        assert st == 200 and r['resultado'] == 'SEGUNDO_CONTEO', r
        cc2 = r['segundo_conteo_id']
        dueno = self.s(cc2).operario_id
        assert dueno != op1.id, 'el CC2 le tocó a quien hizo el CC1'
        if dueno is not None:           # el sistema eligió: cuenta quien le tocó
            from app.models.usuario import Usuario
            op2 = self.db.session.get(Usuario, dueno)
        return cc2, self.contar(op2, cc2, total1 if total2 is None else total2)

    def jobs(self, sid):
        from app.models.siesa_job import SiesaJob
        return SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_tipo='SesionConteo',
                                        referencia_id=sid).all()

    def ejecutar_ajustes(self):
        """El DLQ: ejecuta cada `AJUSTE_CONTEO` pendiente con el gateway real."""
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        from app.services.siesa_job_service import _ejecutar_job
        for job in SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO',
                                            estado=EstadoSiesaJob.PENDIENTE).all():
            antes = len(self.siesa.posts)
            _ejecutar_job(job)
            job.estado = EstadoSiesaJob.COMPLETADO
            self.db.session.commit()
            sid = json.loads(job.payload)['sesion_id']
            nuevos = self.siesa.posts[antes:]
            assert len(nuevos) == 1, f'el job de la sesión {sid} mandó {len(nuevos)} POST'
            self.ejecutados.append((sid, nuevos[0][2]))
        return self.ejecutados

    def wms(self, p, almacen=None):
        from app.services.conteo_service import ConteoService
        self.db.session.expire_all()
        return ConteoService.existencia_wms_del_sku(p.id, (almacen or self.nb1).id)

    def por_lugar(self, p, almacen=None):
        from app.models.inventario import UbicacionProducto
        from app.models.ubicacion import Ubicacion
        self.db.session.expire_all()
        filas = (UbicacionProducto.query.join(Ubicacion)
                 .filter(Ubicacion.almacen_id == (almacen or self.nb1).id,
                         UbicacionProducto.producto_id == p.id).all())
        return {f.ubicacion.codigo: f.cantidad for f in filas}

    # ── el chequeo común ──────────────────────────────────────────────────
    def foto_inicial(self):
        """Saldo de partida por hueco y desde qué movimiento se cuenta."""
        from app.models.inventario import MovimientoInventario, UbicacionProducto
        self.db.session.expire_all()
        self._base = {(f.ubicacion_id, f.producto_id): f.cantidad or 0
                      for f in UbicacionProducto.query.all()}
        self._mov_desde = self.db.session.query(
            self.db.func.coalesce(self.db.func.max(MovimientoInventario.id), 0)).scalar()

    def verificar_inventario(self, *, wms_en_lo_contado=True, auditoria_declarada=()):
        from app.models.conteo import SesionConteo
        from app.models.inventario import MovimientoInventario, UbicacionProducto
        from app.services import auditoria
        self.db.session.expire_all()
        errores = []
        filas = UbicacionProducto.query.all()
        # 1 · nada negativo, nada reservado de más
        for f in filas:
            if (f.cantidad or 0) < 0 or (f.reservado or 0) < 0 or (f.reservado or 0) > (f.cantidad or 0):
                errores.append(f'hueco {f.ubicacion_id} × prod {f.producto_id}: cantidad '
                               f'{f.cantidad}, reservado {f.reservado}')
        # 2 · todo cambio de cantidad dejó su movimiento (con motivo)
        movs = MovimientoInventario.query.filter(MovimientoInventario.id > self._mov_desde).all()
        suma = {}
        for m in movs:
            # El libro tiene dos convenciones de signo (picking escribe SALIDA
            # con cantidad positiva; el ajuste de conteo, negativa): manda el
            # saldo antes → después cuando está, y la cantidad debe coincidir
            # en valor absoluto.
            if m.saldo_antes is not None and m.saldo_despues is not None:
                delta = m.saldo_despues - m.saldo_antes
                if abs(delta) != abs(m.cantidad):
                    errores.append(f'movimiento {m.id} ({m.tipo}): cantidad {m.cantidad} '
                                   f'y saldo {m.saldo_antes}→{m.saldo_despues} no cuadran')
            else:
                delta = m.cantidad
            suma[(m.ubicacion_id, m.producto_id)] = suma.get((m.ubicacion_id, m.producto_id), 0) + delta
            if not (m.motivo or '').strip():
                errores.append(f'movimiento {m.id} ({m.tipo}) sin motivo')
        for f in filas:
            k = (f.ubicacion_id, f.producto_id)
            esperado = self._base.get(k, 0) + suma.get(k, 0)
            if (f.cantidad or 0) != esperado:
                errores.append(f'hueco {k}: cantidad {f.cantidad} ≠ saldo inicial '
                               f'{self._base.get(k, 0)} + movimientos {suma.get(k, 0)}')
        # 3 · lo que salió a Siesa es contado − teórico, una vez, y el WMS quedó en lo contado
        motivos = {'AJ-ENT': ('01',), 'AJ-SAL': ('02',)}
        for sid, pl in self.ejecutados:
            s = self.db.session.get(SesionConteo, sid)
            mov = pl['Movimientos'][0]
            doc = pl['Documentos'][0]
            dif = s.cantidad_fisica - s.teorico_siesa
            signo = 1 if s.motivo_codigo == 'AJ-ENT' else -1
            if signo * mov['f470_cant_base'] != dif:
                errores.append(f'{s.codigo}: 142951 mueve {signo * mov["f470_cant_base"]} y '
                               f'contado − teórico = {dif}')
            if mov['f470_id_motivo'] not in motivos[s.motivo_codigo]:
                errores.append(f'{s.codigo}: motivo {mov["f470_id_motivo"]} para {s.motivo_codigo}')
            if doc['f350_id_tipo_docto'] != 'ADI':
                errores.append(f'{s.codigo}: tipo {doc["f350_id_tipo_docto"]}')
            if len(self.siesa.posts_de(s.codigo)) != 1:
                errores.append(f'{s.codigo}: {len(self.siesa.posts_de(s.codigo))} POST a Siesa')
            if s.estado != 'AJUSTADO':
                errores.append(f'{s.codigo}: {s.estado} después del POST aceptado')
        if wms_en_lo_contado:
            # La última raíz cerrada de cada SKU con foto de Siesa: el WMS quedó en lo contado.
            ultimas = {}
            for s in (SesionConteo.query.filter_by(es_segundo_conteo=False)
                      .order_by(SesionConteo.id).all()):
                ultimas[(s.producto_id, s.almacen_id)] = s
            for s in ultimas.values():
                if s.estado in ('MATCH', 'AJUSTADO') and s.fuente_existencia == 'SIESA' \
                        and s.teorico_siesa is not None:
                    objetivo = s.cantidad_fisica + int(s.cant_pos_siesa or 0)
                    real = self.wms_por_ids(s.producto_id, s.almacen_id)
                    if real != objetivo:
                        errores.append(f'{s.codigo} {s.estado}: el WMS quedó en {real}, '
                                       f'contado {s.cantidad_fisica} + POS {s.cant_pos_siesa or 0} '
                                       f'= {objetivo}')
        # 4 · el auditor de flujo de conteo
        r = auditoria.auditar('conteo')
        for x in r['resultados']:
            if (x['severidad'] == auditoria.BLOQUEA and x['total']
                    and x['codigo'] not in auditoria_declarada):
                errores.append(f'auditoría {x["codigo"]}: {x["hallazgos"][:2]}')
        errores += [f'auditoría {c} reventó' for c in r['errores']]
        assert not errores, '\n'.join(errores)

    def wms_por_ids(self, producto_id, almacen_id):
        from app.services.conteo_service import ConteoService
        return ConteoService.existencia_wms_del_sku(producto_id, almacen_id)


@pytest.fixture
def m(app, client, db, siesa, almacen):
    w = Mundo(app, client, db, siesa, almacen)
    w.ana = w.persona('ana', 'operario', puede_picar=True)
    w.beto = w.persona('beto', 'operario', puede_picar=True)
    w.caro = w.persona('caro', 'operario', puede_picar=True)
    w.sofi = w.persona('sofi', 'supervisor')
    w.saul = w.persona('saul', 'supervisor')
    w.jorge = w.persona('jorge', 'jefe_almacen')
    w.adri = w.persona('adri', 'admin')
    w.tina = w.persona('tina', 'picker_traslado', w.nc1)
    w.toño = w.persona('tono', 'picker_traslado', w.nc1)
    return w


def _mov(pl):
    return pl['Movimientos'][0]


# ═════════════════════════════════════════════════════════════════════════════
# a · Cuadra al primer conteo
# ═════════════════════════════════════════════════════════════════════════════

class TestA_Cuadra:

    def test_match_en_nb1_deja_el_wms_en_lo_contado_sin_tocar_siesa(self, m):
        # El WMS tiene 5 fantasma en CROSS-DOCK; Siesa y el estante, 50.
        p = m.producto(lugares={'SIESA-GENERAL': 40, 'PIK-1': 10, 'CROSS-DOCK': 5}, existencia=50)
        sid = m.manual(m.sofi, p)
        r = m.contar(m.ana, sid, 50)
        assert r['resultado'] == 'MATCH', r
        s = m.s(sid)
        assert (s.estado, s.fuente_existencia, s.teorico_siesa) == ('MATCH', 'SIESA', 50)
        assert m.jobs(sid) == [] and m.siesa.posts == []
        assert m.wms(p) == 50, m.por_lugar(p)
        m.verificar_inventario()

    def test_match_en_tienda_con_pos_pendiente(self, m):
        # NC1: Siesa existencia 23 con 3 de caja sin acumular → teórico 20.
        p = m.producto(m.nc1, {'SIESA-GENERAL': 23}, pos=3)
        sid = m.manual(m.sofi, p, m.nc1)
        t = m.abrir(m.tina, sid)
        assert t['perimetro']['tipo'] == 'TIENDA', t['perimetro']
        st, r = m.confirmar(m.tina, sid, 20)
        assert st == 200 and r['resultado'] == 'MATCH', r
        assert m.siesa.posts == []
        assert m.wms(p, m.nc1) == 23          # contado 20 + POS 3 (la carga de las 7:00 no lo deshace)
        m.verificar_inventario()


# ═════════════════════════════════════════════════════════════════════════════
# b · CC1 = CC2: sobrante y faltante, dentro y fuera del tope; quién firma
# ═════════════════════════════════════════════════════════════════════════════

class TestB_VerdadDeBodega:

    @pytest.mark.parametrize('contado,motivo,siesa_motivo', [(46, 'AJ-SAL', '02'),
                                                              (53, 'AJ-ENT', '01')])
    def test_bajo_el_tope_sale_solo_y_el_payload_es_el_del_spec(self, m, contado, motivo, siesa_motivo):
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.sofi, p)
        cc2, r = m.cc1_cc2(sid, m.ana, m.beto, contado)
        assert m.s(cc2).operario_id == m.beto.id
        assert r['resultado'] == 'DESCUADRE', r
        assert m.s(sid).estado == 'AJUSTANDO' and m.s(sid).aprobador_id is None
        m.verificar_inventario()               # encolado, nada movido todavía
        m.ejecutar_ajustes()
        (_, pl), = m.ejecutados
        doc, mov = pl['Documentos'][0], _mov(pl)
        assert (doc['f350_id_tipo_docto'], doc['f350_id_clase_docto'], doc['f450_id_concepto'],
                doc['f350_id_co'], doc['F_CIA']) == ('ADI', 63, 603, '003', 1)
        assert (mov['f470_id_bodega'], mov['f470_id_motivo'], mov['f470_cant_base'],
                mov['f470_referencia_item'], mov['f470_costo_prom_uni'], mov['f470_id_concepto']) \
            == ('NB1', siesa_motivo, abs(contado - 50), p.codigo, None, 603)
        assert 'f470_desc_varible' in mov and mov['f470_id_ubicacion_aux'] is None
        assert doc['f350_notas'] == m.s(sid).codigo
        assert m.s(sid).estado == 'AJUSTADO' and m.wms(p) == contado
        m.verificar_inventario()

    def test_sobre_el_tope_espera_firma_y_solo_firma_quien_puede(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50}, costo=90000)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 46)              # −4 × $90.000 = $360.000 > $100.000
        s = m.s(sid)
        assert s.estado == 'DESCUADRE' and m.jobs(sid) == []
        assert s.to_dict()['no_sale_solo']['codigo'] == 'SUPERA_TOPE'
        for quien in (m.ana, m.beto, m.jorge):         # operarios y jefe con tope $0
            st, r = m.put(quien, f'/api/conteo/{sid}/ajustar')
            assert st == 403, (quien.nombre, r)
        assert m.jobs(sid) == []
        m.verificar_inventario()
        st, r = m.put(m.sofi, f'/api/conteo/{sid}/ajustar')    # creó el manual, no contó
        assert st == 202, r
        m.ejecutar_ajustes()
        assert _mov(m.ejecutados[0][1])['f470_cant_base'] == 4
        assert m.wms(p) == 46
        m.verificar_inventario()


# ═════════════════════════════════════════════════════════════════════════════
# c · CC1 ≠ CC2 → CC3 del supervisor, ciego, y el CC3 decide
# ═════════════════════════════════════════════════════════════════════════════

class TestC_Definitivo:

    def test_cc3_ciego_de_supervision_decide_la_cifra(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.saul, p)
        cc2, r = m.cc1_cc2(sid, m.ana, m.beto, 45, 47)
        assert r['resultado'] == 'TERCER_CONTEO', r
        cc3 = r['tercer_conteo_id']
        assert m.s(cc3).operario_id is None
        # Ningún operario lo recibe; supervisión lo ve en su cola sin cifras.
        for op in (m.ana, m.beto, m.caro):
            st, t = m.get(op, '/api/mobile/tarea-actual')
            assert t.get('sin_tareas'), (op.nombre, t)
        st, lista = m.get(m.sofi, f'/api/conteo/?almacen_id={m.nb1.id}')
        for fila in lista['sesiones']:
            for campo in ('teorico_siesa', 'existencia_siesa', 'cantidad_fisica', 'diferencia'):
                assert fila.get(campo) in (None,), (fila['id'], campo, fila.get(campo))
        st, cola = m.get(m.sofi, f'/api/conteo/definitivos?almacen_id={m.nb1.id}')
        assert [x['id'] for x in cola['pendientes']] == [cc3]
        r = m.contar(m.sofi, cc3, 48)
        assert r['resultado'] == 'DESCUADRE' and r['raiz_id'] == sid, r
        s = m.s(sid)
        assert (s.estado, s.cantidad_fisica, s.diferencia) == ('DESCUADRE', 48, -2)
        # $2.000 ≤ tope de autoaprobación: quien contó el CC3 puede firmarlo.
        st, r = m.put(m.sofi, f'/api/conteo/{sid}/ajustar')
        assert st == 202, r
        m.ejecutar_ajustes()
        assert (_mov(m.ejecutados[0][1])['f470_id_motivo'], _mov(m.ejecutados[0][1])['f470_cant_base']) == ('02', 2)
        assert m.wms(p) == 48
        m.verificar_inventario()


# ═════════════════════════════════════════════════════════════════════════════
# d · POS
# ═════════════════════════════════════════════════════════════════════════════

class TestD_POS:

    def test_venta_de_caja_mientras_se_cuenta_pide_recontar(self, m):
        p = m.producto(m.nc1, {'SIESA-GENERAL': 30})
        sid = m.manual(m.sofi, p, m.nc1)
        m.abrir(m.tina, sid)                                   # foto de apertura: 30
        m.siesa.poner(p.codigo, 30, bodega='NC1', pos=2)       # vendieron 2 en caja
        st, r = m.confirmar(m.tina, sid, 28)
        assert st == 200 and r['resultado'] == 'RECONTAR', r
        assert set(r) <= {'resultado', 'mensaje', 'sesion_id', 'motivo'}, r
        assert m.jobs(sid) == [] and m.siesa.posts == []
        # Recuenta con la caja quieta: cuadra con el teórico nuevo (30 − 2).
        r = m.contar(m.tina, sid, 28)
        assert r['resultado'] == 'MATCH', r
        assert m.wms(p, m.nc1) == 30                           # 28 + 2 de POS sin acumular
        m.verificar_inventario()

    def test_pos_pendiente_se_descuenta_del_teorico_y_no_nace_ajuste(self, m):
        p = m.producto(m.nc1, {'SIESA-GENERAL': 60}, pos=10)   # teórico 50
        sid = m.manual(m.sofi, p, m.nc1)
        r = m.contar(m.tina, sid, 50)
        assert r['resultado'] == 'MATCH', r
        assert m.s(sid).teorico_siesa == 50 and m.siesa.posts == []
        m.verificar_inventario()

    def test_salida_sin_confirmar_que_no_es_pos_no_ajusta(self, m):
        # 4 und en una remisión sin confirmar (ssc 6, POS 2): no se sabe si salieron.
        p = m.producto(m.nc1, {'SIESA-GENERAL': 40}, pos=2, salida_sin_conf=6)
        sid = m.manual(m.sofi, p, m.nc1)
        cc2, r = m.cc1_cc2(sid, m.tina, m.toño, 34)            # teórico 38
        s = m.s(sid)
        assert s.estado == 'DESCUADRE' and m.jobs(sid) == []
        assert 'NO son venta POS' in (s.to_dict().get('bloqueo_ajuste') or ''), s.to_dict().get('bloqueo_ajuste')
        st, r = m.put(m.sofi, f'/api/conteo/{sid}/ajustar')
        assert st == 400 and m.jobs(sid) == [], r
        m.verificar_inventario(wms_en_lo_contado=False)

    def test_el_aviso_de_cajas_pos_es_de_tienda_no_del_cd(self, m):
        a = m.producto(lugares={'SIESA-GENERAL': 5})
        b = m.producto(m.nc1, {'SIESA-GENERAL': 5})
        t_cd = m.abrir(m.ana, m.manual(m.sofi, a))
        t_tienda = m.abrir(m.tina, m.manual(m.sofi, b, m.nc1))
        assert (t_cd['perimetro']['tipo'], t_tienda['perimetro']['tipo']) == ('CD', 'TIENDA')
        m.verificar_inventario()


# ═════════════════════════════════════════════════════════════════════════════
# e · Pedido comprometido: reservado, recogido, empacado, remisionado, muelle
# ═════════════════════════════════════════════════════════════════════════════

def _pedido(m, p, cantidad=10):
    from tests.flujo.conductor_de_flujo import Flujo, sembrar_pedido
    numero = sembrar_pedido(m.db, [p], cantidad=cantidad)
    return Flujo(pedido=numero, almacen_id=m.nb1.id, usuario_id=m.ana.id, producto_ids=[p.id])


class TestE_PedidoEnCurso:

    def test_reservado_sin_recoger_se_cuenta_y_cuadra(self, m):
        from app.services.picking_service import PickingService
        p = m.producto(lugares={'PIK-1': 50})
        f = _pedido(m, p)
        PickingService.crear_tareas(producto_id=p.id, cantidad=10, almacen_id=m.nb1.id,
                                    referencia_documento=f.pedido, tipo_documento='PEDIDO')
        m.db.session.commit()
        m.verificar_inventario()
        sid = m.manual(m.sofi, p)
        r = m.contar(m.caro, sid, 50)
        assert r['resultado'] == 'MATCH', r
        assert m.siesa.posts == []
        m.verificar_inventario()

    @pytest.mark.parametrize('hasta', ['recogido', 'empacado'])
    def test_recogido_sin_remision_no_nace_ajuste(self, m, hasta):
        from tests.flujo.conductor_de_flujo import hacer_packing, hacer_picking
        p = m.producto(lugares={'PIK-1': 50})
        f = _pedido(m, p)
        with m.simulando():
            hacer_picking(m.db, f, 10, 10)             # 10 al carro; Siesa sigue en 50
            if hasta == 'empacado':
                hacer_packing(m.db, f)                 # caja cerrada, sin remisión todavía
        assert m.wms(p) == 40
        m.verificar_inventario()
        sid = m.manual(m.sofi, p)
        cc2, r = m.cc1_cc2(sid, m.beto, m.caro, 40)
        s = m.s(sid)
        assert s.estado == 'DESCUADRE' and m.jobs(sid) == [], s.estado
        assert f.pedido in (s.to_dict().get('bloqueo_ajuste') or '')
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 400 and m.jobs(sid) == [] and m.siesa.posts == [], r
        m.verificar_inventario(wms_en_lo_contado=False)

    @pytest.mark.parametrize('muelle', ['caja_cerrada', 'camion_sin_salir', 'camion_salio'])
    def test_empacado_con_remision_no_se_cuenta_y_cuadra(self, m, muelle):
        from app.models.bulto import Bulto
        from tests.flujo.conductor_de_flujo import (hacer_packing, hacer_picking, hacer_ruta,
                                                    siesa_emitio)
        p = m.producto(lugares={'PIK-1': 50})
        f = _pedido(m, p)
        with m.simulando():
            hacer_picking(m.db, f, 10, 10)
            hacer_packing(m.db, f)
            siesa_emitio(m.db, f.packing_id)
        m.siesa.poner(p.codigo, 40)                   # la remisión descargó Siesa
        if muelle != 'caja_cerrada':
            ruta = hacer_ruta(m.db, f, m.jorge.id)
            for b in Bulto.query.filter(Bulto.id.in_(f.bultos)).all():
                b.estado = 'CARGADO'
            ruta.estado = 'EN_TRANSITO' if muelle == 'camion_salio' else 'EN_CARGUE'
            m.db.session.commit()
        m.verificar_inventario()
        sid = m.manual(m.sofi, p)
        t = m.abrir(m.ana, sid)
        assert t['empacado_por_salir'] is (muelle != 'camion_salio'), (muelle, t['empacado_por_salir'])
        st, r = m.confirmar(m.ana, sid, 40)          # el estante, sin las cajas
        assert st == 200 and r['resultado'] == 'MATCH', r
        assert m.siesa.posts == []
        m.verificar_inventario()


    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO P2: el perímetro «no cuente lo empacado» es solo un texto del HUD. '
        'Con cajas remisionadas esperando en el muelle (empacado_por_salir=True), '
        'si CC1 y CC2 las cuentan igual, sale un AJ-ENT automático por las '
        'unidades de las cajas (bajo el tope): Siesa sube 10 que ya salieron. '
        'Ninguna guarda de motivo_no_sale_solo / motivo_bloqueo_ajuste '
        '(conteo_service.py:455 / :3035) mira empacado_por_salir en un sobrante.'))
    def test_contar_las_cajas_del_muelle_no_ajusta_solo(self, m):
        from tests.flujo.conductor_de_flujo import hacer_packing, hacer_picking, siesa_emitio
        p = m.producto(lugares={'PIK-1': 50})
        f = _pedido(m, p)
        with m.simulando():
            hacer_picking(m.db, f, 10, 10)
            hacer_packing(m.db, f)
            siesa_emitio(m.db, f.packing_id)
        m.siesa.poner(p.codigo, 40)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 50)            # los dos cuentan las cajas
        assert m.jobs(sid) == [], 'nació un AJ-ENT por mercancía ya remisionada'


# ═════════════════════════════════════════════════════════════════════════════
# f · Traslado en tránsito y recepción sin entrada en Siesa
# ═════════════════════════════════════════════════════════════════════════════

class TestF_EnCamino:

    def test_traslado_despachado_sin_ets_no_ajusta_la_tienda(self, m):
        from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
        p = m.producto(m.nc1, {'SIESA-GENERAL': 20})
        st_ = SolicitudTraslado(codigo='ST-E2E-001', bodega_origen_siesa='NB1',
                                bodega_destino_siesa='NC1', estado='EN_TRANSITO',
                                solicitante_id=m.sofi.id, siesa_salida_consec=77,
                                fecha_despacho=datetime.utcnow() - timedelta(hours=2))
        m.db.session.add(st_)
        m.db.session.flush()
        m.db.session.add(ItemSolicitudTraslado(solicitud_id=st_.id, producto_id=p.id,
                                               cantidad_solicitada=5))
        m.db.session.commit()
        sid = m.manual(m.sofi, p, m.nc1)
        # Las cajas del traslado llegaron y la tienda las contó: 25 contra 20.
        m.cc1_cc2(sid, m.tina, m.toño, 25)
        s = m.s(sid)
        assert s.estado == 'DESCUADRE' and m.jobs(sid) == []
        assert 'ST-E2E-001' in (s.to_dict().get('bloqueo_ajuste') or '')
        assert m.siesa.posts == []
        m.verificar_inventario(wms_en_lo_contado=False)

    def test_recepcion_sin_entrada_oc_no_ajusta(self, m):
        from app.models.recepcion import ItemRecepcion, RecepcionMercancia
        # Llegó la OC: 10 und al estante (WMS 60 con su movimiento), Siesa sin EntradaOC (50).
        p = m.producto(lugares={'SIESA-GENERAL': 60}, existencia=50)
        rec = RecepcionMercancia(codigo='REC-E2E-1', numero_oc_siesa='OC-99', almacen_id=m.nb1.id,
                                 estado='CONFIRMADA', fecha_inicio=datetime.utcnow() - timedelta(hours=1),
                                 siesa_triggered=False)
        m.db.session.add(rec)
        m.db.session.flush()
        m.db.session.add(ItemRecepcion(recepcion_id=rec.id, producto_id=p.id, cantidad_ordenada=10,
                                       cantidad_recibida=10))
        m.db.session.commit()
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 60)
        s = m.s(sid)
        assert s.estado == 'DESCUADRE' and m.jobs(sid) == [] and m.siesa.posts == []
        assert 'REC-E2E-1' in (s.to_dict().get('bloqueo_ajuste') or '') \
            or 'OC-99' in (s.to_dict().get('bloqueo_ajuste') or ''), s.to_dict().get('bloqueo_ajuste')
        m.verificar_inventario(wms_en_lo_contado=False)


# ═════════════════════════════════════════════════════════════════════════════
# g · Un SKU en cuatro lugares: una cadena, el ajuste repartido por hueco
# ═════════════════════════════════════════════════════════════════════════════

class TestG_VariosLugares:

    LUGARES = {'SIESA-GENERAL': 10, 'PIK-1': 20, 'RES-1': 30, 'CROSS-DOCK': 40}

    def test_una_cadena_un_total_y_el_faltante_repartido(self, m):
        from app.models.conteo import SesionConteo
        from app.models.inventario import MovimientoInventario, UbicacionProducto
        p = m.producto(lugares=self.LUGARES)
        # Lo reservado para un pedido en CROSS-DOCK se toca al final.
        up = (UbicacionProducto.query.filter_by(producto_id=p.id,
              ubicacion_id=m.ub(m.nb1, 'CROSS-DOCK').id).one())
        up.reservado = 15
        m.db.session.commit()
        sid = m.manual(m.sofi, p)
        st, r = m.post(m.sofi, '/api/conteo/manual', {'almacen_id': m.nb1.id, 'producto_codigo': p.codigo})
        assert SesionConteo.query.filter_by(producto_id=p.id, es_segundo_conteo=False).count() == 1, r
        t = m.abrir(m.ana, sid)
        assert len([l for l in t['lugares'] if l['fisica']]) == 3 and t['lugares'][-1]['fisica'] is False
        m.confirmar(m.ana, sid, 70)
        st, r = m.confirmar(m.ana, sid, 70)
        m.contar(m.beto, r['segundo_conteo_id'], 70)          # CC1 = CC2 = 70 → AJ-SAL 30
        m.ejecutar_ajustes()
        assert _mov(m.ejecutados[0][1])['f470_cant_base'] == 30
        lugares = m.por_lugar(p)
        assert lugares == {'SIESA-GENERAL': 0, 'PIK-1': 20, 'RES-1': 30, 'CROSS-DOCK': 20}, lugares
        movs = MovimientoInventario.query.filter_by(producto_id=p.id, tipo='AJUSTE_CONTEO').all()
        assert sorted(x.cantidad for x in movs) == [-20, -10]
        m.verificar_inventario()

    def test_el_sobrante_entra_a_la_ubicacion_de_la_cadena(self, m):
        p = m.producto(lugares=self.LUGARES)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 103)
        m.ejecutar_ajustes()
        assert m.por_lugar(p)['SIESA-GENERAL'] == 13 and m.wms(p) == 103
        m.verificar_inventario()


# ═════════════════════════════════════════════════════════════════════════════
# h · Siesa sin fila (= 0), la entrada con costo (clase 61 / 601) y sin costo (409)
# ═════════════════════════════════════════════════════════════════════════════

class TestH_SinFila:

    def _sin_fila_contado(self, m, contado=12, costo_otra=1500.0):
        p = m.producto(lugares={'CROSS-DOCK': 12}, en_siesa=False)
        if costo_otra:
            m.siesa.poner(p.codigo, 10, bodega='NS1', costo=costo_otra)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, contado)
        return p, sid

    def test_la_entrada_nunca_sale_sola_y_la_firma_el_admin(self, m):
        p, sid = self._sin_fila_contado(m)
        s = m.s(sid)
        assert (s.estado, s.teorico_siesa, s.sin_fila_en_siesa) == ('DESCUADRE', 0, True)
        assert m.jobs(sid) == []
        st, r = m.put(m.saul, f'/api/conteo/{sid}/ajustar')   # supervisor que no contó
        assert st == 403, r
        st, r = m.get(m.adri, f'/api/conteo/{sid}/costo-sugerido')
        assert st == 200, r
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 202, r
        m.ejecutar_ajustes()
        (_, pl), = m.ejecutados
        doc, mov = pl['Documentos'][0], _mov(pl)
        assert (doc['f350_id_clase_docto'], doc['f450_id_concepto'], mov['f470_id_concepto'],
                mov['f470_id_motivo'], mov['f470_cant_base'], mov['f470_costo_prom_uni']) \
            == (61, 601, 601, '01', 12, 1500.0)
        # El WMS del SKU queda en 24 (defecto declarado abajo, xfail).
        m.verificar_inventario(wms_en_lo_contado=False)

    def test_sin_costo_en_ninguna_fuente_pide_el_costo(self, m):
        p, sid = self._sin_fila_contado(m, costo_otra=None)
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 409 and r.get('requiere_costo'), r
        assert m.jobs(sid) == [] and m.s(sid).estado == 'DESCUADRE'
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar', {'costo_unitario': 800})
        assert st == 202, r
        m.ejecutar_ajustes()
        assert _mov(m.ejecutados[0][1])['f470_costo_prom_uni'] == 800.0
        m.verificar_inventario(wms_en_lo_contado=False)

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO P1: con Siesa sin fila el WMS ya tenía las unidades (por eso se '
        'contó), y el job AJUSTE_CONTEO aplica el delta de Siesa (+12) encima: '
        'estante 12, Siesa 12, WMS 24. Si el lugar de la cadena es físico, la '
        'carga de las 7:00 no lo corrige (solo pone en cero SIESA-GENERAL). '
        'aplicar_ajuste_al_wms (conteo_service.py:985) suma el delta de Siesa en '
        'vez de dejar el SKU en lo contado; solo el MATCH lo hace (cuadrar_wms_con_lo_contado).'))
    def test_tras_la_entrada_el_wms_queda_en_lo_contado(self, m):
        p, sid = self._sin_fila_contado(m)
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 202, r
        m.ejecutar_ajustes()
        assert m.wms(p) == 12, m.por_lugar(p)

    def test_contado_cero_contra_siesa_sin_fila_limpia_el_fantasma(self, m):
        p = m.producto(lugares={'CROSS-DOCK': 9}, en_siesa=False)
        sid = m.manual(m.sofi, p)
        r = m.contar(m.ana, sid, 0)
        assert r['resultado'] == 'MATCH', r
        assert m.wms(p) == 0 and m.siesa.posts == []
        m.verificar_inventario()

    def test_referencia_fuera_del_maestro_no_es_cero(self, m):
        p = m.producto(lugares={'CROSS-DOCK': 9}, en_siesa=False)
        m.siesa.maestro.discard(p.codigo)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 4)
        s = m.s(sid)
        assert s.fuente_existencia == 'WMS' and s.estado == 'DESCUADRE' and m.jobs(sid) == []
        m.verificar_inventario(wms_en_lo_contado=False)


# ═════════════════════════════════════════════════════════════════════════════
# i · Siesa caído, foto vieja, zombi
# ═════════════════════════════════════════════════════════════════════════════

class TestI_SiesaYElTiempo:

    def test_siesa_caido_al_abrir_se_cuenta_pero_no_ajusta(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.sofi, p)
        m.siesa.caida = True
        m.abrir(m.ana, sid)
        m.siesa.caida = False
        st, r = m.confirmar(m.ana, sid, 46)
        assert r['resultado'] == 'RECONTAR_TU', r
        st, r = m.confirmar(m.ana, sid, 46)
        cc2 = r['segundo_conteo_id']
        # El CC2 con sus dos fotos SÍ ajustaría (su observación reemplaza la del
        # CC1): para que la cadena quede sin foto de apertura, el segundo
        # también abre con Siesa caído.
        m.siesa.caida = True
        m.abrir(m.beto, cc2)
        m.siesa.caida = False
        st, r = m.confirmar(m.beto, cc2, 46)
        assert st == 200, r
        s = m.s(sid)
        assert s.estado == 'DESCUADRE' and m.jobs(sid) == [], s.estado
        assert 'APERTURA' in (s.to_dict().get('bloqueo_ajuste') or '')
        m.verificar_inventario(wms_en_lo_contado=False)

    def test_siesa_caido_al_cerrar_compara_contra_el_wms_y_no_ajusta(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 30, 'PIK-1': 20})
        sid = m.manual(m.sofi, p)
        m.abrir(m.ana, sid)
        m.siesa.caida = True
        st, r = m.confirmar(m.ana, sid, 50)
        assert st == 200 and r['resultado'] == 'MATCH', r
        assert m.s(sid).fuente_existencia == 'WMS' and m.siesa.posts == []
        m.verificar_inventario()

    def test_foto_vieja_otro_conteo_posterior_la_invalida(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50}, costo=90000)
        viejo = m.manual(m.sofi, p)
        m.cc1_cc2(viejo, m.ana, m.beto, 46)            # sobre el tope: espera firma
        assert m.s(viejo).estado == 'DESCUADRE'
        nuevo = m.manual(m.sofi, p)                   # recontar (lo que pide el líder)
        assert nuevo != viejo
        m.contar(m.caro, nuevo, 50)                   # cuadró: el faltante era falso
        assert m.s(nuevo).estado == 'MATCH'
        st, r = m.put(m.adri, f'/api/conteo/{viejo}/ajustar')
        assert st == 400 and m.jobs(viejo) == [] and m.siesa.posts == [], r
        m.verificar_inventario()

    def test_el_zombi_vuelve_a_la_cola_desde_cero_con_rastro(self, m):
        from app.services.conteo_service import ConteoService
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.sofi, p)
        m.abrir(m.ana, sid)
        st, r = m.post(m.ana, '/api/mobile/conteo/total', {'tarea_id': sid, 'total_acumulado': 17})
        assert st == 200, r
        s = m.s(sid)
        s.ultima_actividad_at = datetime.utcnow() - timedelta(hours=3)
        m.db.session.commit()
        assert ConteoService.liberar_tareas_zombi() == 1
        s = m.s(sid)
        assert (s.estado, s.operario_id, s.cantidad_fisica) == ('PENDIENTE', None, None)
        assert s.conteos_descartados and '17' in json.dumps(s.conteos_descartados)
        r = m.contar(m.beto, sid, 50)
        assert r['resultado'] == 'MATCH', r
        m.verificar_inventario()


# ═════════════════════════════════════════════════════════════════════════════
# j · Asignación por presencia
# ═════════════════════════════════════════════════════════════════════════════

class TestJ_Asignacion:

    def test_reparto_ausencia_regreso_y_supervisor_solo_cc3(self, m):
        from app.services import asignacion, presencia
        from app.models.conteo import SesionConteo
        dani = m.persona('dani', 'operario', puede_picar=True, senal=False)   # sin señal
        prods = [m.producto(lugares={'SIESA-GENERAL': 10}) for _ in range(6)]
        ids = [m.manual(m.sofi, p) for p in prods]
        presencia.declarar_ausencia(m.caro.id, 'INCAPACIDAD', por_id=m.sofi.id)
        st, r = m.post(m.sofi, '/api/conteo/asignar-lote', {'almacen_id': m.nb1.id})
        assert st == 200, r
        duenos = {m.s(i).operario_id for i in ids}
        assert duenos <= {m.ana.id, m.beto.id} and duenos, (duenos, r)
        # El supervisor no recibe rutinarios aunque la cola tenga.
        st, t = m.get(m.sofi, '/api/mobile/tarea-actual')
        assert t.get('sin_tareas') or t.get('id') not in ids, t
        # Beto se va: lo suyo no empezado vuelve a la cola.
        de_beto = [i for i in ids if m.s(i).operario_id == m.beto.id]
        presencia.declarar_ausencia(m.beto.id, 'PERMISO', por_id=m.sofi.id)
        assert all(m.s(i).operario_id is None for i in de_beto), de_beto
        # Caro vuelve (el líder anula la ausencia) y entra al reparto.
        from app.models.ausencia import AusenciaUsuario
        a = AusenciaUsuario.query.filter_by(usuario_id=m.caro.id).one()
        presencia.anular_ausencia(a.id, por_id=m.sofi.id, motivo='vino')
        m.caro.ultima_senal_at = datetime.utcnow()
        m.db.session.commit()
        st, r = m.post(m.sofi, '/api/conteo/asignar-lote', {'almacen_id': m.nb1.id})
        assert m.caro.id in {m.s(i).operario_id for i in ids}, r
        assert dani.id not in {m.s(i).operario_id for i in ids}
        assert m.beto.id not in {m.s(i).operario_id for i in ids}
        m.verificar_inventario()

    def test_conteo_manual_a_quien_no_tiene_senal_si_a_ausente_no(self, m):
        from app.services import asignacion, presencia
        dani = m.persona('dani', 'operario', puede_picar=True, senal=False)
        p = m.producto(lugares={'SIESA-GENERAL': 10})
        sid = m.manual(m.sofi, p, operario=dani)
        assert m.s(sid).operario_id == dani.id
        asignacion.barrer()
        assert m.s(sid).operario_id == dani.id           # el barrido no se lo quita
        presencia.declarar_ausencia(m.caro.id, 'VACACIONES', por_id=m.sofi.id)
        q = m.producto(lugares={'SIESA-GENERAL': 10})
        st, r = m.post(m.sofi, '/api/conteo/manual', {'almacen_id': m.nb1.id,
                                                      'producto_codigo': q.codigo,
                                                      'operario_id': m.caro.id})
        assert st == 404, r
        r = m.contar(dani, sid, 10)
        assert r['resultado'] == 'MATCH', r
        m.verificar_inventario()

    def test_el_cc2_nunca_a_quien_hizo_el_cc1_ni_a_supervision(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.sofi, p)
        m.contar(m.ana, sid, 40)
        st, r = m.confirmar(m.ana, sid, 40)
        cc2 = m.s(r['segundo_conteo_id'])
        assert cc2.operario_id in (m.beto.id, m.caro.id), cc2.operario_id
        st, r2 = m.get(m.ana, f'/api/conteo/{cc2.id}/tarea')
        assert st == 400, r2
        m.verificar_inventario(wms_en_lo_contado=False)


# ═════════════════════════════════════════════════════════════════════════════
# k · Omitir, editar, el único admin
# ═════════════════════════════════════════════════════════════════════════════

class TestK_Excepciones:

    def test_omitir_el_segundo_conteo_con_motivo_y_otro_firma(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.sofi, p)
        m.contar(m.ana, sid, 47)
        m.confirmar(m.ana, sid, 47)
        st, r = m.post(m.sofi, f'/api/conteo/{sid}/omitir-segundo', {})
        assert st == 400, r
        st, r = m.post(m.sofi, f'/api/conteo/{sid}/omitir-segundo', {'motivo': 'no hay segundo operario'})
        assert st == 200, r
        s = m.s(sid)
        assert s.estado == 'DESCUADRE' and m.jobs(sid) == []
        st, r = m.put(m.sofi, f'/api/conteo/{sid}/ajustar')    # quien omitió no firma
        assert st == 403, r
        st, r = m.put(m.saul, f'/api/conteo/{sid}/ajustar')
        assert st == 202, r
        m.ejecutar_ajustes()
        assert m.wms(p) == 47
        # CNT-04 lo marca BLOQUEA (defecto declarado abajo, xfail).
        m.verificar_inventario(auditoria_declarada=('CNT-04',))

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO P2: omitir el 2º conteo con motivo y que firme otra persona es '
        'el camino sancionado por C2 (ConteoService.omitir_verificacion), pero '
        'CNT-04 (auditoria/conteo.py) lo reporta como BLOQUEA «ajustado con '
        'diferencia sin segundo conteo»: cada omisión legítima deja la Salud en '
        'NO_CONFIABLE. Debería eximir la raíz con verificacion_omitida_* (o bajar a AVISA).'))
    def test_la_omision_sancionada_no_es_un_bloqueante(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.sofi, p)
        m.contar(m.ana, sid, 47)
        m.confirmar(m.ana, sid, 47)
        m.post(m.sofi, f'/api/conteo/{sid}/omitir-segundo', {'motivo': 'no hay segundo operario'})
        m.put(m.saul, f'/api/conteo/{sid}/ajustar')
        m.ejecutar_ajustes()
        m.verificar_inventario()

    def test_la_cifra_confirmada_no_se_edita(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 50}, costo=90000)
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 46)
        for cifra in (48, 50):
            st, r = m.put(m.adri, f'/api/conteo/{sid}/editar',
                          {'cantidad_fisica': cifra, 'motivo_edicion': 'me equivoqué'})
            assert st == 409, r
        assert (m.s(sid).cantidad_fisica, m.s(sid).estado) == (46, 'DESCUADRE')
        m.verificar_inventario(wms_en_lo_contado=False)

    def test_unico_admin_que_conto_un_cc3_sobre_el_tope(self, m):
        p = m.producto(lugares={'SIESA-GENERAL': 500}, costo=90000)
        sid = m.manual(m.sofi, p)
        cc2, r = m.cc1_cc2(sid, m.ana, m.beto, 480, 490)
        cc3 = r['tercer_conteo_id']
        r = m.contar(m.adri, cc3, 470)               # −30 × $90.000 = $2,7 M
        st, r = m.put(m.adri, f'/api/conteo/{sid}/ajustar')
        assert st == 403 and 'otro admin' in r['error'], r
        st, r = m.put(m.sofi, f'/api/conteo/{sid}/ajustar')
        assert st == 403, r
        assert m.jobs(sid) == [] and m.siesa.posts == []
        m.verificar_inventario(wms_en_lo_contado=False)


# ═════════════════════════════════════════════════════════════════════════════
# Encontrado por la corrida real (ensayo): MODO_ENSAYO cierra el ajuste
# ═════════════════════════════════════════════════════════════════════════════

class TestModoEnsayo:

    @pytest.mark.xfail(strict=True, reason=(
        'DEFECTO P2: en MODO_ENSAYO el 142951 no sale (el gateway devuelve '
        '{modo_ensayo: True}), _ejecutar_con_preflag baja la bandera, pero la '
        'rama AJUSTE_CONTEO de _ejecutar_job (siesa_job_service.py:1510 y :1527; y si el job se re-ejecuta, la guarda de :1362 le pone siesa_triggered=True) '
        'sigue: pone la sesión AJUSTADO, guarda la respuesta de ensayo como '
        'desenlace y mueve el WMS (aplicar_ajuste_al_wms). Siesa no cambió y el '
        'WMS sí: en QA (.env.qa trae MODO_ENSAYO=true) cada ajuste de conteo '
        'descuadra el WMS en silencio. Visto en la corrida de '
        'scripts/qa_conteo_escenarios_real.py sin --si-de-verdad (WMS 1741, '
        'Siesa 1739, sesión AJUSTADO). Los traslados ya lo resolvieron '
        '(TrasladoNoEnviadoEnEnsayo).'))
    def test_un_ajuste_no_enviado_no_se_da_por_ajustado(self, m, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        monkeypatch.setattr(ConnektaGateway, '_post',
                            lambda self, c, n, payload, url=None, extra_params=None:
                            {'modo_ensayo': True, 'conector': c,
                             'mensaje': 'POST bloqueado en modo ensayo'})
        p = m.producto(lugares={'SIESA-GENERAL': 50})
        sid = m.manual(m.sofi, p)
        m.cc1_cc2(sid, m.ana, m.beto, 46)
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _ejecutar_job
        (job,) = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=sid).all()
        _ejecutar_job(job)
        s = m.s(sid)
        assert s.estado != 'AJUSTADO', 'la sesión quedó AJUSTADO sin que el 142951 saliera'
        assert m.wms(p) == 50, 'el WMS se movió con Siesa intacta'
