"""
Las tres puertas del anti-duplicado de factura electrónica.

`get_factura_desde_pedido` existe para una sola cosa: que nadie emita una
segunda FE sobre un pedido que ya la tiene. Su propio comentario dice que
**no devuelve `[]` ante error de red** porque el caller lo leería como «no hay
factura previa». Aun así, el guard estaba abierto por tres lados distintos:

1. **La excepción se tragaba.** `pedido_closer.py` hacía
   `except (FutTimeout, Exception)` y anotaba «continuando (DLQ reconciliará
   si hay conflicto)». El DLQ no reconcilia nada: aguas abajo encola
   DESPACHO_F470, que ejecuta 244328 → 142945 (remisión, descarga inventario)
   → 142943 (FE). Cuando el DLQ mira, los documentos ya existen. Y el
   presupuesto del precheck son 8 s contra los 30 del GET, así que el camino
   de fallo se dispara **cada vez que Siesa va lento** — justo cuando el
   intento anterior quedó a medias.

2. **El sobre de rechazo nunca fue excepción.** Connekta contesta HTTP 200 con
   `[{'alerta': ...}]`. Esa fila no trae `f350_ind_estado`, el default `'9'`
   la marcaba como anulada, el filtro la quitaba, y la función devolvía `[]`.
   Sin excepción y sin log, con el fail-fast intacto al lado.

3. `SKIP_FE_CHECK=true`, que ya estaba declarada como peligrosa en
   `vars_criticas` y no se toca acá.

Las dos primeras se cierran en este archivo. La tercera es configuración.

## Y la de la nota crédito, que era la misma forma

`NOTA_CREDITO_DEVOLUCION_CLIENTE` marcaba `siesa_nc_triggered` **después** del
POST. Un timeout entre el POST y el commit dejaba la bandera abajo, el DLQ
reintentaba, y Siesa recibía una segunda nota crédito.

`CLAUDE.md` declara ese defecto corregido y lista el job como «pre-flag». Lo
estaba en `NOTA_CREDITO_FACTURA` —que **no tiene productor**—: el arreglo se
había aplicado al gemelo muerto.
"""
import pytest

from app.services.connekta_gateway import (
    ConnektaConsultaRechazada,
    ConnektaResultadoDesconocido,
)

_SOBRE = {'detalle': {'Table': [{'alerta': 'Por favor verifique los parámetros'}]}}


@pytest.fixture
def gw(monkeypatch):
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'modo_simulacion', False)
    monkeypatch.delenv('SKIP_FE_CHECK', raising=False)
    return connekta


class TestPuerta2ElSobreDeRechazo:
    """Es la que estaba abierta con el fail-fast escrito al lado."""

    @pytest.mark.parametrize('metodo,args', [
        ('get_factura_desde_pedido', ('PD', 1352)),
        ('get_factura_desde_remision', ('RM', 4001)),
    ])
    def test_un_rechazo_no_se_lee_como_sin_factura(self, gw, monkeypatch,
                                                    metodo, args):
        monkeypatch.setattr(gw, '_get', lambda *a, **k: _SOBRE)
        with pytest.raises(Exception) as e:
            getattr(gw, metodo)(*args)
        assert 'rechazó la consulta' in str(e.value) or 'No se pudo' in str(e.value), (
            f'{metodo} devolvió algo en vez de levantar: el caller lo leería '
            f'como «no hay factura previa» y emitiría la segunda')

    @pytest.mark.parametrize('metodo,args', [
        ('get_factura_desde_pedido', ('PD', 1352)),
        ('get_factura_desde_remision', ('RM', 4001)),
    ])
    def test_una_factura_real_sigue_pasando(self, gw, monkeypatch, metodo, args):
        """El otro lado: si el guard empezara a levantar siempre, nadie podría
        despachar. `f350_ind_estado='1'` es una FE viva."""
        monkeypatch.setattr(gw, '_get', lambda *a, **k: {'detalle': {'Table': [
            {'f350_ind_estado': '1', 'f350_consec_docto': 1466}]}})
        assert len(getattr(gw, metodo)(*args)) == 1

    @pytest.mark.parametrize('metodo,args', [
        ('get_factura_desde_pedido', ('PD', 1352)),
        ('get_factura_desde_remision', ('RM', 4001)),
    ])
    def test_una_anulada_se_sigue_descartando(self, gw, monkeypatch, metodo, args):
        """`'9'` es anulada: no bloquea un despacho nuevo. Es la razón por la
        que el default existía — el arreglo no puede habérselo comido."""
        monkeypatch.setattr(gw, '_get', lambda *a, **k: {'detalle': {'Table': [
            {'f350_ind_estado': '9', 'f350_consec_docto': 1466}]}})
        assert getattr(gw, metodo)(*args) == []

    @pytest.mark.parametrize('metodo,args', [
        ('get_factura_desde_pedido', ('PD', 1352)),
        ('get_factura_desde_remision', ('RM', 4001)),
    ])
    def test_sin_facturas_devuelve_vacio(self, gw, monkeypatch, metodo, args):
        monkeypatch.setattr(gw, '_get', lambda *a, **k: {'detalle': {'Table': []}})
        assert getattr(gw, metodo)(*args) == []


class TestPuerta1ElPrecheckQueSeguiaDeLargo:
    def _tarea(self, db, almacen):
        from app.models.packing import TareaPacking
        t = TareaPacking(codigo='PK-PRECHK', estado='VERIFICADO',
                         almacen_id=almacen.id, numero_pedido_siesa='PD-PRECHK',
                         tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa='1352')
        db.session.add(t)
        db.session.commit()
        return t

    def test_si_no_se_puede_preguntar_el_cierre_se_detiene(self, db, almacen,
                                                           monkeypatch):
        """**El detector ciego.** Antes esto seguía y emitía los documentos."""
        from app.services.closing.pedido_closer import PedidoPackingCloser
        from app.services.connekta_gateway import connekta
        t = self._tarea(db, almacen)
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(connekta, 'get_estado_pedido', lambda *a, **k: 2)

        def _boom(*a, **k):
            raise Exception('Connekta no respondió')
        monkeypatch.setattr(connekta, 'get_factura_desde_pedido', _boom)

        error = PedidoPackingCloser()._precheck_siesa(t)
        assert error is not None, (
            'el precheck devolvió None —«seguí»— sin haber podido preguntar '
            'si el pedido ya tiene factura')
        assert 'duplicada' in error

    def test_con_siesa_sana_y_sin_factura_previa_sigue(self, db, almacen,
                                                        monkeypatch):
        """Lo que el arreglo no puede romper: el camino normal."""
        from app.services.closing.pedido_closer import PedidoPackingCloser
        from app.services.connekta_gateway import connekta
        t = self._tarea(db, almacen)
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(connekta, 'get_estado_pedido', lambda *a, **k: 2)
        monkeypatch.setattr(connekta, 'get_factura_desde_pedido',
                            lambda *a, **k: [])
        assert PedidoPackingCloser()._precheck_siesa(t) is None

    def test_una_factura_previa_sigue_deteniendo(self, db, almacen, monkeypatch):
        from app.services.closing.pedido_closer import PedidoPackingCloser
        from app.services.connekta_gateway import connekta
        t = self._tarea(db, almacen)
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(connekta, 'get_estado_pedido', lambda *a, **k: 2)
        monkeypatch.setattr(connekta, 'get_factura_desde_pedido',
                            lambda *a, **k: [{'f350_id_tipo_docto': 'FEW',
                                              'f350_consec_docto': 1466}])
        error = PedidoPackingCloser()._precheck_siesa(t)
        assert error is not None and 'ya tiene factura' in error


class TestElPreFlagDeLaNotaCreditoViva:
    """El gemelo que sí corre. `NOTA_CREDITO_FACTURA` no tiene productor."""

    def test_ningun_productor_encola_el_gemelo_muerto(self):
        """Si algún día alguien lo reconecta, hay que revisar los dos, no uno."""
        import pathlib
        import re
        hits = []
        for p in pathlib.Path('app').rglob('*.py'):
            for n, l in enumerate(p.read_text().split('\n'), 1):
                if re.search(r"tipo\s*=\s*'NOTA_CREDITO_FACTURA'", l):
                    hits.append(f'{p}:{n}')
        assert not hits, (
            f'apareció un productor de NOTA_CREDITO_FACTURA en {hits}. Ese '
            f'handler estaba muerto; si vuelve a la vida hay que darle el '
            f'mismo trato de pre-flag que al de devolución.')

    def test_marca_la_bandera_ANTES_del_post(self):
        """Por AST: el `siesa_nc_triggered = True` del handler vivo tiene que
        estar antes de la llamada a `trigger_nota_factura_crear_cruzar`.

        Es la Regla 6, y este handler la incumplía: marcaba después, así que
        un timeout entre el POST y el commit dejaba la bandera abajo y el DLQ
        reintentaba sobre una NC que ya existía.
        """
        import ast
        import pathlib
        src = pathlib.Path('app/services/siesa_job_service.py').read_text()
        arbol = ast.parse(src)

        # el bloque `if job.tipo == 'NOTA_CREDITO_DEVOLUCION_CLIENTE'`
        bloque = None
        for n in ast.walk(arbol):
            if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                    and any(isinstance(c, ast.Constant)
                            and c.value == 'NOTA_CREDITO_DEVOLUCION_CLIENTE'
                            for c in n.test.comparators)):
                bloque = n
                break
        assert bloque is not None, 'no se encontró el handler vivo'

        post = [x.lineno for x in ast.walk(bloque)
                if isinstance(x, ast.Call)
                and getattr(x.func, 'attr', None) == 'trigger_nota_factura_crear_cruzar']
        marcas = [x.lineno for x in ast.walk(bloque)
                  if isinstance(x, ast.Assign)
                  and any(getattr(t, 'attr', None) == 'siesa_nc_triggered'
                          for t in x.targets)
                  and isinstance(x.value, ast.Constant) and x.value.value is True]
        assert post, 'el handler ya no llama al conector'
        assert marcas, 'el handler no marca siesa_nc_triggered en ningún lado'
        assert min(marcas) < min(post), (
            f'la bandera se marca en la línea {min(marcas)} y el POST está en '
            f'la {min(post)}: sigue siendo post-flag. Un timeout entre los dos '
            f'deja la bandera abajo y el DLQ crea la segunda nota crédito.')

    def test_un_resultado_desconocido_no_revierte(self, db, almacen, monkeypatch):
        """Con la bandera abajo el DLQ reintenta sobre una NC que puede
        existir. **El caso que el tipo de excepción vino a distinguir.**"""
        import json

        from app.models.devolucion_cliente import DevolucionCliente
        from app.models.packing import TareaPacking
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        from app.services.connekta_gateway import connekta

        alm = almacen
        t = TareaPacking(codigo='PK-NCVIVA', estado='DESPACHADO',
                         almacen_id=alm.id, numero_pedido_siesa='PD-NCVIVA',
                         tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa='700', siesa_triggered=True)
        db.session.add(t)
        db.session.flush()
        d = DevolucionCliente(codigo='DEVC-VIVA', tarea_packing_id=t.id,
                              numero_pedido_siesa='PD-NCVIVA',
                              tipo_docto_fe='FEW', consec_fe='1466',
                              almacen_id=alm.id, estado='CONFIRMADA')
        db.session.add(d)
        db.session.commit()
        job = SiesaJob(tipo='NOTA_CREDITO_DEVOLUCION_CLIENTE', estado='PENDIENTE',
                       referencia_tipo='DevolucionCliente', referencia_id=d.id,
                       payload=json.dumps({
                           'devolucion_id': d.id,
                           'tipo_docto_fe': 'FEW',
                           'consec_fe': 1466,
                           # El handler vivo SIEMPRE arma por match de ítems
                           # (nunca la rama es_total), así que el payload
                           # necesita `items_devueltos` con el mismo código
                           # que trae `f120_referencia` en los rowids.
                           'items_devueltos': [{'codigo': 'SKU-NC',
                                                'cantidad_devuelta': 1}],
                       }))
        db.session.add(job)
        db.session.commit()
        did = d.id

        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [
            {'f470_rowid': 1, 'f470_vlr_neto': 1000, 'f470_cant_base': 1,
             'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
             'f120_referencia': 'SKU-NC'}])

        def _timeout(*a, **k):
            raise ConnektaResultadoDesconocido('sin respuesta')
        monkeypatch.setattr(connekta, 'trigger_nota_factura_crear_cruzar', _timeout)

        with pytest.raises(ConnektaResultadoDesconocido):
            sjs._ejecutar_job(job)

        assert DevolucionCliente.query.get(did).siesa_nc_triggered is True, (
            'el pre-flag se revirtió ante un resultado DESCONOCIDO — el '
            'siguiente ciclo del DLQ crea la segunda nota crédito')
