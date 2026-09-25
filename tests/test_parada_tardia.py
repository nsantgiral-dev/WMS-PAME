"""
Una ruta ENTREGADA con una parada sin gestionar tiene salida (P1-4) y el
cierre forzado no liquida (P1-5). 2026-09-25.

## Lo que pasaba

La cola sin señal del conductor mandaba el cierre de la ruta aunque una
confirmación de parada hubiera fallado (`rutas.js`: «un ítem con error no debe
congelar los demás»). La ruta quedaba ENTREGADA con esa parada sin recaudo, y:

- confirmarla exigía EN_TRANSITO (la ruta y el servicio);
- forzar el cierre, también;
- liquidar exige todas las paradas gestionadas.

Sin salida. Y `forzar_cierre_ruta` marcaba la ruta LIQUIDADA con
`_marcar_liquidada`, saltándose las guardas de `liquidar_ruta` (crédito no
autorizado, devoluciones sin contar).

## Ahora

- `entregar_ruta` **declara** las paradas sin gestionar (no se niega: un
  cierre trabado en el teléfono no lo destraba nadie).
- La cola del conductor no manda el cierre de una ruta con una parada suya que
  no salió.
- Una parada tardía entra con la ruta ENTREGADA (no LIQUIDADA): la oficina con
  motivo; la primera confirmación que llega por la cola, con el motivo que
  pone el sistema. FORZAR en la bitácora.
- Forzar el cierre sirve sobre una ruta ENTREGADA y la deja ENTREGADA.
"""
import json
import pathlib
import shutil
import subprocess
import uuid

import pytest
from flask_jwt_extended import create_access_token

RAIZ = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def mundo(db, almacen):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from tests.flujo import conductor_de_flujo as cf
    op = Usuario(email=f'op_{uuid.uuid4().hex[:5]}@t.co', nombre='Op', rol='operario', activo=True)
    uc = Usuario(email=f'co_{uuid.uuid4().hex[:5]}@t.co', nombre='Cond', rol='conductor', activo=True)
    ad = Usuario(email=f'ad_{uuid.uuid4().hex[:5]}@t.co', nombre='Admin', rol='admin', activo=True)
    for u in (op, uc, ad):
        u.set_password('x')
        db.session.add(u)
    db.session.flush()
    c = Conductor(nombre='Cond', cedula=f'C{uuid.uuid4().hex[:6]}', usuario_id=uc.id, activo=True)
    db.session.add(c)
    db.session.commit()
    productos, _ = cf.sembrar_catalogo(db, almacen)
    pedido = cf.sembrar_pedido(db, productos)
    f = cf.Flujo(pedido=pedido, almacen_id=almacen.id, usuario_id=uc.id,
                 producto_ids=[p.id for p in productos])
    cf.hacer_picking(db, f, 10, 7)
    cf.hacer_packing(db, f)
    cf.hacer_ruta(db, f, c.id)
    return f, uc, ad


def _h(app, u):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


_ENTREGA = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'CREDITO', 'monto_cobrado': 0}


class TestEntregarDeclaraLoQueFalta:

    def test_la_ruta_cierra_y_dice_que_parada_quedo_sin_gestionar(self, db, mundo):
        from app.services.ruta_service import RutaService
        f, uc, _ = mundo
        res = RutaService.entregar_ruta(f.ruta_id, {'bultos': []}, uc.id)
        assert res['ruta']['estado'] == 'ENTREGADA'
        assert [p['tarea_id'] for p in res['paradas_sin_gestionar']] == [f.packing_id]


class TestLaParadaTardia:

    def _entregada(self, db, f, uc):
        from app.services.ruta_service import RutaService
        RutaService.entregar_ruta(f.ruta_id, {'bultos': []}, uc.id)

    def test_sin_motivo_no_entra(self, db, mundo):
        from app.services.ruta_service import RutaService
        f, uc, _ = mundo
        self._entregada(db, f, uc)
        with pytest.raises(ValueError, match='motivo'):
            RutaService.confirmar_parada(f.ruta_id, f.packing_id, uc.id, dict(_ENTREGA))

    def test_con_motivo_entra_y_queda_en_la_bitacora(self, db, mundo):
        from app.models.bitacora import BitacoraAccion
        from app.services.bitacora import FORZADO_PARADA_TARDIA
        from app.services.ruta_service import RutaService
        f, uc, _ = mundo
        self._entregada(db, f, uc)
        RutaService.confirmar_parada(f.ruta_id, f.packing_id, uc.id, dict(_ENTREGA),
                                     motivo_tardia='la cola no la mandó')
        [b] = BitacoraAccion.query.filter_by(accion='FORZAR').all()
        assert b.despues['forzado'] == FORZADO_PARADA_TARDIA and b.motivo == 'la cola no la mandó'

    def test_una_ruta_liquidada_no_se_toca(self, db, mundo):
        from app.models.ruta_despacho import RutaDespacho
        from app.services.ruta_service import RutaService
        f, uc, _ = mundo
        self._entregada(db, f, uc)
        ruta = db.session.get(RutaDespacho, f.ruta_id)
        ruta.estado_financiero = 'LIQUIDADA'
        db.session.commit()
        with pytest.raises(ValueError, match='liquidada'):
            RutaService.confirmar_parada(f.ruta_id, f.packing_id, uc.id, dict(_ENTREGA),
                                         motivo_tardia='x')

    def test_la_cola_del_conductor_entra_sola_la_primera_vez(self, app, client, db, mundo):
        from app.models.bitacora import BitacoraAccion
        f, uc, _ = mundo
        self._entregada(db, f, uc)
        url = f'/api/rutas/{f.ruta_id}/paradas/{f.packing_id}/confirmar'
        sin_cola = client.post(url, headers=_h(app, uc), json=dict(_ENTREGA))
        assert sin_cola.status_code == 400
        r = client.post(url, headers=_h(app, uc), json={**_ENTREGA, 'via_cola': True})
        assert r.status_code == 200, r.get_json()
        [b] = BitacoraAccion.query.filter_by(accion='FORZAR').all()
        assert 'cola' in b.motivo
        # Una SEGUNDA vez ya no: corregir después del cierre es de la oficina.
        otra = client.post(url, headers=_h(app, uc),
                           json={**_ENTREGA, 'via_cola': True, 'observaciones': 'x'})
        assert otra.status_code == 400

    def test_la_oficina_la_registra_con_motivo(self, app, client, db, mundo):
        f, uc, ad = mundo
        self._entregada(db, f, uc)
        url = f'/api/rutas/{f.ruta_id}/paradas/{f.packing_id}/confirmar'
        assert client.post(url, headers=_h(app, ad), json=dict(_ENTREGA)).status_code == 400
        r = client.post(url, headers=_h(app, ad),
                        json={**_ENTREGA, 'motivo_tardia': 'el conductor la confirmó por teléfono'})
        assert r.status_code == 200, r.get_json()


class TestElCierreForzadoNoLiquida:

    def test_sobre_una_ruta_entregada_cierra_lo_que_falta_y_no_liquida(self, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        RutaService.entregar_ruta(f.ruta_id, {'bultos': []}, uc.id)
        res = RutaService.forzar_cierre_ruta(f.ruta_id, ad.id, motivo='no volvió a aparecer')
        assert res['paradas_auto_cerradas'] == 1
        ruta = db.session.get(RutaDespacho, f.ruta_id)
        assert ruta.estado == 'ENTREGADA' and ruta.estado_financiero != 'LIQUIDADA'
        assert RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one().estado_entrega == 'RECHAZADO'

    def test_despues_la_liquidacion_pasa_por_sus_guardas(self, db, mundo):
        """La parada dada por rechazada deja una devolución sin contar: liquidar
        la exige (o un motivo). Antes el cierre forzado ya había liquidado."""
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        RutaService.forzar_cierre_ruta(f.ruta_id, ad.id, motivo='sin señal')
        with pytest.raises(ValueError, match='devoluciones_sin_contar'):
            RutaService.liquidar_ruta(f.ruta_id, usuario_id=ad.id)


# ═════════════════════════════════════════════════════════════════════════════
# La cola del conductor no cierra una ruta con una parada que no salió
# ═════════════════════════════════════════════════════════════════════════════

def _node(tmp_path, js):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    from tests.test_sin_codigos_en_pantalla import _node as _n
    return _n(tmp_path, ['util.js', 'rutas.js'], {}, js)


class TestLaColaNoCierraConUnaParadaPendiente:

    def test_el_cierre_espera_a_la_parada_que_fallo(self, tmp_path):
        out = _node(tmp_path, """
            const items = [
              { id: 1, tipo: 'confirmar', rutaId: 7, tareaId: 3, payload: {} },
              { id: 2, tipo: 'cerrar', rutaId: 7, payload: { bultos: [] } },
              { id: 3, tipo: 'cerrar', rutaId: 8, payload: { bultos: [] } },
            ];
            const pedidos = [], sacados = [];
            _condDB.queue = async () => items;
            _condDB.dequeue = async (id) => { sacados.push(id); };
            fetch = async (url) => {
              pedidos.push(url);
              if (url.includes('/paradas/')) return { ok: false, status: 400, json: async () => ({ error: 'no' }) };
              return { ok: true, status: 200, json: async () => ({}) };
            };
            _COND_RUTA_ACTIVA = null;
            cargarRutasConductor = async () => {};
            await condSyncQueue();
            return { pedidos: JSON.stringify(pedidos), sacados: JSON.stringify(sacados) };
        """)
        pedidos = json.loads(out['pedidos'])
        assert not any(u.endswith('/api/rutas/7/entregar') for u in pedidos), pedidos
        assert any(u.endswith('/api/rutas/8/entregar') for u in pedidos), 'otra ruta sí cierra'
        assert json.loads(out['sacados']) == [3]
