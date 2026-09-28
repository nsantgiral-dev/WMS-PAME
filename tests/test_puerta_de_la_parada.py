"""
La puerta de la confirmación de una parada (validación de la plata, 2026-09-26).

## La clase

*Una puerta de oficina que reescribe lo confirmado por el conductor sin el
permiso de corregir cobros.* La puerta de la parada tardía
(`puede_registrar_parada_tardia`: admin + liquidador) aceptaba cualquier
parada de una ruta cerrada: el liquidador convertía un ENTREGADO en efectivo en
«no pagó y se quedó» sin `puede_corregir_cobro` (admin + líder de cartera).
Con la ruta en camino, la oficina registraba por el conductor sin motivo, sin
marca y sin bitácora. Y la confirmación real del conductor que llegaba después
de un cierre forzado rebotaba (400) y se perdía.

## Ahora

`parada_tardia.puerta_de_confirmacion` decide (la ruta y el servicio la llaman):

- registrar una parada **sin gestionar**: ruta cerrada → quien liquida; en
  camino → quien corrige cobros. Motivo, formulario vigente, FORZAR, marca.
- **corregir** una ya registrada: quien corrige cobros, con motivo, FORZAR
  `parada_confirmada_corregida_por_la_oficina`. Sirve también después de
  cerrar la llegada si no contradice lo físico.
- la oficina con el formulario abierto sobre una parada que entretanto
  confirmó el conductor: **409**, no pisa.
- el cierre forzado marca sus recaudos como de la oficina: lo que mande el
  teléfono después se guarda aparte.

## El trinquete

Toda función de `app/` que escribe `estado_entrega`, `forma_pago` o
`monto_cobrado` de un recaudo está inventariada con la guarda que la protege, y
la llama. El inventario solo encoge; cada entrada dice por qué.
"""
import ast
import pathlib
from types import SimpleNamespace

import pytest

from tests.test_cartera_retencion import _jwt, _usuario
from tests.test_parada_tardia import mundo  # noqa: F401 — fixture
from tests.test_parada_de_oficina import FOTO, _cerrar, _url

RAIZ = pathlib.Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La matriz — quién hace qué, sobre qué parada (la función pura)
# ═════════════════════════════════════════════════════════════════════════════

def _ruta(estado, liquidada=False):
    return SimpleNamespace(estado=estado, estado_financiero='LIQUIDADA' if liquidada else None)


def _u(rol):
    return SimpleNamespace(rol=rol, activo=True)


_PREVIA = SimpleNamespace(registrada_por_oficina=None)
_V = {'version_formulario': 4}

#: (situación, estado ruta, previa, kwargs) → quién entra. **A mano.**
MATRIZ = [
    ('registrar tardía', 'ENTREGADA', None, {'motivo_tardia': 'x'}, {'admin', 'liquidador'}),
    ('registrar en camino', 'EN_TRANSITO', None, {'motivo_tardia': 'x'}, {'admin', 'lider_cartera'}),
    ('corregir tras el cierre', 'ENTREGADA', _PREVIA, {'motivo_correccion': 'x'},
     {'admin', 'lider_cartera'}),
    ('corregir en camino', 'EN_TRANSITO', _PREVIA, {'motivo_correccion': 'x'},
     {'admin', 'lider_cartera'}),
    ('sin intención, parada registrada', 'ENTREGADA', _PREVIA, {'motivo_correccion': None},
     {'admin', 'lider_cartera'}),
]
ROLES = ('admin', 'liquidador', 'lider_cartera', 'jefe_almacen', 'gerente', 'supervisor',
         'operario')


class TestLaMatriz:

    @pytest.mark.parametrize('nombre,estado,previa,kw,pasan', MATRIZ, ids=[m[0] for m in MATRIZ])
    def test_quien_entra(self, nombre, estado, previa, kw, pasan):
        from app.services import parada_tardia as pt
        entran = set()
        for rol in ROLES:
            kwargs = {k: v for k, v in kw.items() if v is not None}
            try:
                pt.puerta_de_confirmacion(_ruta(estado), previa, _u(rol), es_conductor=False,
                                          data=dict(_V), **kwargs)
                entran.add(rol)
            except pt.PuertaCerrada as e:
                # Atravesar la puerta del permiso: todo lo que no es 403 (un
                # 400 por el motivo que falta ya es «adentro»).
                if e.status != 403:
                    entran.add(rol)
        assert entran == pasan, nombre

    def test_la_oficina_que_llega_tarde_no_pisa_409(self):
        from app.services import parada_tardia as pt
        for rol, estado in (('liquidador', 'ENTREGADA'), ('admin', 'ENTREGADA'),
                            ('lider_cartera', 'EN_TRANSITO')):
            with pytest.raises(pt.PuertaCerrada) as e:
                pt.puerta_de_confirmacion(_ruta(estado), _PREVIA, _u(rol), es_conductor=False,
                                          data=dict(_V), motivo_tardia='x')
            assert e.value.status == 409, rol

    def test_toda_escritura_de_la_oficina_pide_motivo_y_formulario_vigente(self):
        from app.services import parada_tardia as pt
        for previa, kw in ((None, {'motivo_tardia': '  '}), (_PREVIA, {'motivo_correccion': ''})):
            with pytest.raises(pt.PuertaCerrada, match='motivo'):
                pt.puerta_de_confirmacion(_ruta('ENTREGADA'), previa, _u('admin'),
                                          es_conductor=False, data=dict(_V), **kw)
            with pytest.raises(pt.PuertaCerrada, match='desactualizado'):
                pt.puerta_de_confirmacion(_ruta('ENTREGADA'), previa, _u('admin'),
                                          es_conductor=False, data={'version_formulario': 3},
                                          **{k: 'x' for k in kw})

    def test_el_conductor(self):
        from app.services import parada_tardia as pt
        p = pt.puerta_de_confirmacion
        assert p(_ruta('EN_TRANSITO'), _PREVIA, None, es_conductor=True, data={})['modo'] == pt.CONDUCTOR
        assert p(_ruta('ENTREGADA'), None, None, es_conductor=True,
                 data={'via_cola': True})['modo'] == pt.CONDUCTOR_COLA_TARDIA
        for previa, data in ((_PREVIA, {'via_cola': True}), (None, {})):
            with pytest.raises(pt.PuertaCerrada):
                p(_ruta('ENTREGADA'), previa, None, es_conductor=True, data=data)

    def test_una_ruta_liquidada_no_la_abre_nadie(self):
        from app.services import parada_tardia as pt
        for es_c, u in ((True, None), (False, _u('admin'))):
            for estado in ('EN_TRANSITO', 'ENTREGADA'):
                with pytest.raises(pt.PuertaCerrada, match='liquidada'):
                    pt.puerta_de_confirmacion(_ruta(estado, liquidada=True), None, u,
                                              es_conductor=es_c, data=dict(_V),
                                              motivo_tardia='x')


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Por HTTP — la corrección tiene rastro, el cierre forzado tiene salida
# ═════════════════════════════════════════════════════════════════════════════

def _valor(db, f):
    from app.models.packing import TareaPacking
    return float(db.session.get(TareaPacking, f.packing_id).valor_factura or 0) or 1000.0


def _efectivo(v):
    return {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': v,
            'version_formulario': 4, 'observaciones': 'entregado'}


class TestLaCorreccion:

    def test_el_lider_corrige_con_motivo_y_queda_en_la_bitacora(self, app, client, db, mundo):
        from app.models.bitacora import BitacoraAccion
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.bitacora import FORZADO_PARADA_CORREGIDA
        f, uc, ad = mundo
        v = _valor(db, f)
        assert client.post(_url(f), headers=_jwt(app, uc), json=_efectivo(v)).status_code == 200
        _cerrar(db, f, uc)
        lider = _usuario(db, 'lider_cartera')
        cuerpo = {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO_SE_QUEDO',
                  'observaciones': 'el cliente no pagó', 'foto_entrega': FOTO,
                  'version_formulario': 4}
        sin = client.post(_url(f), headers=_jwt(app, lider),
                          json={**cuerpo, 'motivo_correccion': ' '})
        assert sin.status_code == 400 and 'motivo' in sin.get_json()['error']
        r = client.post(_url(f), headers=_jwt(app, lider),
                        json={**cuerpo, 'motivo_correccion': 'El cliente llamó: no pagó'})
        assert r.status_code == 200, r.get_json()
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.estado_entrega == 'ENTREGADO_SIN_PAGO' and rec.registrada_por_oficina
        [b] = [x for x in BitacoraAccion.query.filter_by(accion='FORZAR').all()
               if x.despues.get('forzado') == FORZADO_PARADA_CORREGIDA]
        assert b.usuario_id == lider.id, [(x.despues, x.motivo) for x in BitacoraAccion.query.all()]
        assert b.antes['estado_entrega'] == 'ENTREGADO' and b.motivo == 'El cliente llamó: no pagó'

    def test_una_correccion_no_desdice_lo_que_volvio(self, app, client, db, mundo):
        """El camión trajo el bulto (RETORNADO): decir «se entregó» contradice
        lo físico, con permiso y motivo o sin ellos."""
        from app.models.bulto import Bulto, EstadoBulto
        f, uc, ad = mundo
        r = client.post(_url(f), headers=_jwt(app, uc), json={
            'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO', 'observaciones': 'x',
            'version_formulario': 4})
        assert r.status_code == 200, r.get_json()
        for b in Bulto.query.filter_by(ruta_despacho_id=f.ruta_id).all():
            b.estado = EstadoBulto.RETORNADO
        db.session.commit()
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json={**_efectivo(_valor(db, f)), 'motivo_correccion': 'sí pagó'})
        assert r.status_code == 400 and 'Lo físico manda' in r.get_json()['error']


class TestElCierreForzadoTieneSalida:

    def test_la_version_del_conductor_se_guarda_y_la_correccion_cierra_el_faltante(
            self, app, client, db, mundo):
        from app.models.bulto import Bulto, EstadoBulto
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services import devolucion_ruta as dr
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        v = _valor(db, f)
        _cerrar(db, f, uc)
        RutaService.forzar_cierre_ruta(f.ruta_id, ad.id, motivo='sin señal, no se sabe')
        dr.cerrar_llegada(f.ruta_id, ad.id)               # recepción: no volvió nada
        assert {b.estado for b in Bulto.query.filter_by(ruta_despacho_id=f.ruta_id)} == {
            EstadoBulto.FALTANTE}
        # La confirmación real llega por la cola: no rebota, va aparte.
        r = client.post(_url(f), headers=_jwt(app, uc), json={**_efectivo(v), 'via_cola': True})
        assert r.status_code == 200 and r.get_json()['version_conductor']['difiere'] is True
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.estado_entrega == 'RECHAZADO' and rec.diferencia_conductor is True
        # El liquidador no la corrige; el líder sí, con motivo.
        liq = _usuario(db, 'liquidador')
        assert client.post(_url(f), headers=_jwt(app, liq),
                           json={**_efectivo(v), 'motivo_correccion': 'x'}).status_code == 403
        lider = _usuario(db, 'lider_cartera')
        r = client.post(_url(f), headers=_jwt(app, lider),
                        json={**_efectivo(v), 'motivo_correccion': 'El conductor sí entregó y cobró'})
        assert r.status_code == 200, r.get_json()
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.estado_entrega == 'ENTREGADO' and float(rec.monto_cobrado) == v
        # Lo que recepción no encontró no faltó: quedó con el cliente.
        assert {b.estado for b in Bulto.query.filter_by(ruta_despacho_id=f.ruta_id)} == {
            EstadoBulto.ENTREGADO}
        assert dr.devolucion_vigente(rec.id) is None

    def test_un_faltante_total_contado_tambien_se_cierra(self, app, client, db, mundo):
        from app.models.devolucion_cliente import EstadoDevolucionCliente as E
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services import devolucion_ruta as dr
        from app.services.devolucion_cliente_service import cambiar_estado
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        RutaService.forzar_cierre_ruta(f.ruta_id, ad.id, motivo='sin señal')
        dr.cerrar_llegada(f.ruta_id, ad.id)
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        d = dr.devolucion_vigente(rec.id)
        cambiar_estado(d, E.FALTANTE_TOTAL)               # bodega contó cero
        db.session.commit()
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json={**_efectivo(_valor(db, f)), 'motivo_correccion': 'sí la entregó'})
        assert r.status_code == 200, r.get_json()
        db.session.refresh(d)
        assert d.estado == E.CANCELADA


class TestLaVersionDelConductorTieneSalida:
    """Validación e2e (2026-09-26): «el conductor mandó otra versión» quedaba
    para siempre («Revíselo con él») y la señal se contradecía con «no la
    confirmó». Ahora: una sola señal, y la salida `parada_tardia.resolver_version`
    (MANTENER / ADOPTAR con motivo; adoptar antes de que salga a Siesa lo hace
    quien liquida, después es corregir el cobro)."""

    def _registrada_y_otra_version(self, app, client, db, f, uc, ad):
        _cerrar(db, f, uc)
        r = client.post(_url(f), headers=_jwt(app, ad), json={
            'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO', 'observaciones': 'x',
            'motivo_tardia': 'lo dijo el cliente', 'version_formulario': 4})
        assert r.status_code == 200, r.get_json()
        r = client.post(_url(f), headers=_jwt(app, uc),
                        json={**_efectivo(_valor(db, f)), 'via_cola': True})
        assert r.get_json()['version_conductor']['difiere'] is True

    def _url_v(self, f, rid):
        return f'/api/rutas/{f.ruta_id}/recaudos/{rid}/version-conductor'

    def test_una_sola_senal(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.senales_ruta import senales_de_recaudo
        f, uc, ad = mundo
        self._registrada_y_otra_version(app, client, db, f, uc, ad)
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        claves = [s['clave'] for s in senales_de_recaudo(rec)]
        assert 'diferencia_con_el_conductor' in claves and 'registrada_por_oficina' not in claves

    def test_mantener_con_motivo(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        f, uc, ad = mundo
        self._registrada_y_otra_version(app, client, db, f, uc, ad)
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        liq = _usuario(db, 'liquidador')
        sin = client.post(self._url_v(f, rec.id), headers=_jwt(app, liq),
                          json={'decision': 'MANTENER', 'motivo': ' '})
        assert sin.status_code == 400
        r = client.post(self._url_v(f, rec.id), headers=_jwt(app, liq),
                        json={'decision': 'MANTENER', 'motivo': 'El cliente confirmó que no pagó'})
        assert r.status_code == 200, r.get_json()
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.diferencia_conductor is False and rec.estado_entrega == 'RECHAZADO'
        assert rec.version_conductor['revision']['decision'] == 'MANTENER'

    def test_adoptar_antes_de_siesa_lo_hace_quien_liquida(self, app, client, db, mundo):
        from app.models.bitacora import BitacoraAccion
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.bitacora import FORZADO_PARADA_CORREGIDA
        f, uc, ad = mundo
        self._registrada_y_otra_version(app, client, db, f, uc, ad)
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        liq = _usuario(db, 'liquidador')
        r = client.post(self._url_v(f, rec.id), headers=_jwt(app, liq),
                        json={'decision': 'ADOPTAR', 'motivo': 'El conductor tenía razón'})
        assert r.status_code == 200, r.get_json()
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.estado_entrega == 'ENTREGADO' and rec.forma_pago == 'EFECTIVO'
        assert rec.diferencia_conductor is False
        assert any(b.despues.get('forzado') == FORZADO_PARADA_CORREGIDA and b.usuario_id == liq.id
                   for b in BitacoraAccion.query.filter_by(accion='FORZAR').all())
        assert 'foto_entrega' not in (rec.to_dict()['version_conductor'].get('cuerpo') or {})

    def test_adoptar_despues_de_siesa_es_corregir_el_cobro(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.siesa_job import SiesaJob
        f, uc, ad = mundo
        self._registrada_y_otra_version(app, client, db, f, uc, ad)
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        SiesaJob.encolar(tipo='DOCUMENTO_CONTABLE_RET', payload={'recaudo_id': rec.id},
                         referencia_tipo='RecaudoEntrega', referencia_id=rec.id)
        db.session.commit()
        liq = _usuario(db, 'liquidador')
        r = client.post(self._url_v(f, rec.id), headers=_jwt(app, liq),
                        json={'decision': 'ADOPTAR', 'motivo': 'x'})
        assert r.status_code == 403 and 'corregir el cobro' in r.get_json()['error']
        # El líder entra, y lo que ya está en cola no se reescribe.
        lider = _usuario(db, 'lider_cartera')
        r = client.post(self._url_v(f, rec.id), headers=_jwt(app, lider),
                        json={'decision': 'ADOPTAR', 'motivo': 'x'})
        assert r.status_code == 400 and 'ya no se puede cambiar' in r.get_json()['error']


# ═════════════════════════════════════════════════════════════════════════════
# 3 · El trinquete — toda escritura del cobro de una parada tiene su guarda
# ═════════════════════════════════════════════════════════════════════════════

CAMPOS = {'estado_entrega', 'forma_pago', 'monto_cobrado'}

#: (módulo, función) → (la guarda que tiene que llamar, o None; por qué).
#: **Solo encoge.**
INVENTARIO = {
    ('app/services/ruta_service.py', 'confirmar_parada'): (
        'puerta_de_confirmacion',
        'la confirmación de la parada: conductor, oficina que registra, oficina que corrige'),
    ('app/services/ruta_service.py', 'forzar_cierre_ruta'): (
        None,
        'crea recaudos solo para paradas SIN recaudo (no reescribe nada) y los marca de la '
        'oficina: lo que mande el teléfono después va aparte'),
    ('app/services/liquidacion_service.py', 'corregir_monto_declarado'): (
        'exigir_correccion_de_monto',
        'corregir el monto (admin + líder de cartera en la ruta), con razón y tope'),
    ('app/services/analitica_recorrido.py', '_evaluar'): (
        None, 'escribe `Pedido.monto_cobrado` de la analítica del recorrido, no un recaudo'),
}


def _escrituras(fuente: str):
    """{función de primer nivel (o método): [líneas]} que escriben CAMPOS: por
    asignación (también en tupla y aumentada), `setattr(x, 'campo', …)` o el
    constructor `RecaudoEntrega(campo=…)`. Docstrings y comentarios no cuentan."""
    arbol = ast.parse(fuente)
    salida = {}

    def _es_campo(t):
        if isinstance(t, ast.Attribute) and t.attr in CAMPOS:
            return True
        if isinstance(t, (ast.Tuple, ast.List)):
            return any(_es_campo(e) for e in t.elts)
        return False

    def _visitar(fn):
        for n in ast.walk(fn):
            hit = False
            if isinstance(n, ast.Assign):
                hit = any(_es_campo(t) for t in n.targets)
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                hit = _es_campo(n.target)
            elif isinstance(n, ast.Call):
                nombre = getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)
                if nombre == 'setattr' and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
                        and n.args[1].value in CAMPOS:
                    hit = True
                if nombre == 'RecaudoEntrega' and any(k.arg in CAMPOS for k in n.keywords):
                    hit = True
            if hit:
                salida.setdefault(fn.name, []).append(n.lineno)

    for nodo in arbol.body:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _visitar(nodo)
        elif isinstance(nodo, ast.ClassDef):
            for m in nodo.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _visitar(m)
    return salida


def _llama(fuente: str, funcion: str, guarda: str) -> bool:
    arbol = ast.parse(fuente)
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == funcion:
            return any(isinstance(c, ast.Call)
                       and (getattr(c.func, 'id', None) or getattr(c.func, 'attr', None)) == guarda
                       for c in ast.walk(n))
    return False


def _todas():
    out = {}
    for p in sorted((RAIZ / 'app').rglob('*.py')) + sorted((RAIZ / 'flota').rglob('*.py')):
        rel = str(p.relative_to(RAIZ))
        for fn in _escrituras(p.read_text(encoding='utf-8')):
            out[(rel, fn)] = p
    return out


class TestTodaEscrituraDelCobroTieneSuGuarda:

    def test_el_inventario_es_exacto(self):
        vistas = set(_todas())
        assert vistas - set(INVENTARIO) == set(), (
            'Una función nueva escribe el estado, la forma de pago o el monto de una '
            'parada: pásela por `parada_tardia.puerta_de_confirmacion` (o por la política '
            'de corrección) y declárela acá con su guarda', sorted(vistas - set(INVENTARIO)))
        assert set(INVENTARIO) - vistas == set(), (
            'Una entrada del inventario ya no escribe: sáquela (solo encoge)',
            sorted(set(INVENTARIO) - vistas))

    def test_cada_una_llama_a_su_guarda_y_dice_por_que(self):
        for (rel, fn), (guarda, por_que) in INVENTARIO.items():
            assert len(por_que) > 20, (rel, fn)
            if guarda:
                assert _llama((RAIZ / rel).read_text(encoding='utf-8'), fn, guarda), (rel, fn, guarda)

    def test_la_ruta_no_decide_por_su_cuenta(self):
        """La ruta HTTP no vuelve a calcular quién entra con los permisos sueltos:
        así fue como la puerta de la parada tardía se abrió de más."""
        fuente = (RAIZ / 'app/routes/rutas.py').read_text(encoding='utf-8')
        arbol = ast.parse(fuente)
        [fn] = [n for n in ast.walk(arbol)
                if isinstance(n, ast.FunctionDef) and n.name == 'confirmar_parada']
        nombres = {getattr(c.func, 'id', None) or getattr(c.func, 'attr', None)
                   for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert not nombres & {'puede_registrar_parada_tardia', 'puede_corregir_cobro'}, nombres
        assert 'confirmar_parada' in nombres
        # La ruta solo decide si se atraviesa la puerta (el guard de rol);
        # qué se puede hacer adentro, el servicio.
        guard = {getattr(a, 'id', None) for c in ast.walk(fn) if isinstance(c, ast.Call)
                 and getattr(c.func, 'id', None) == '_con_permiso' for a in c.args}
        assert guard == {'puede_escribir_parada_desde_oficina'}, guard


class TestElDetectorMuerde:

    def test_ve_las_formas(self):
        src = '''
def a(r):
    r.monto_cobrado = 1
def b(r):
    r.estado_entrega, r.x = 'E', 2
def c(r):
    r.monto_cobrado += 1
def d(r):
    setattr(r, 'forma_pago', 'EFECTIVO')
def e():
    return RecaudoEntrega(estado_entrega='RECHAZADO')
class K:
    def m(self, r):
        r.forma_pago = None
'''
        assert set(_escrituras(src)) == {'a', 'b', 'c', 'd', 'e', 'm'}

    def test_no_marca_lo_sano(self):
        src = '''
def a(r):
    """r.monto_cobrado = 1"""
    # r.estado_entrega = 'X'
    x = r.monto_cobrado
    if r.estado_entrega == 'E':
        return {'monto_cobrado': 1}
def b():
    return RecaudoEntrega.query.filter_by(estado_entrega='E')
'''
        assert _escrituras(src) == {}

    def test_piso(self):
        assert len(_todas()) >= 3
