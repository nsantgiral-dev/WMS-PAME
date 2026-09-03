"""
Dos claves incompletas, el mismo defecto: indexar por una parte de la PK.

## A — el valor unitario de la parada colapsaba (`ruta_service._valor_y_cond_pago`)

Un producto de doble unidad sale en la factura como **dos líneas con la misma
`f120_referencia`**, una en PQ y otra en UND (PAPELSP6741, documentado en
`despacho_parcial_service.py:102-106` junto al error 244328 que ya costó). El
dict `valores_por_referencia` se indexaba **solo por referencia**, así que la
última línea —la de UND— pisaba a la de PQ y el valor unitario se dividía por
el factor de empaque: 2.425 en vez de 24.250.

Lo que lo hacía invisible: `valor_factura` **suma bien** (75.175). El total se
ve correcto y nada parece roto. El unitario, en cambio, viaja hasta
`rutas.js:2199`, que es donde el conductor calcula el descuento de una
devolución en la puerta del cliente — un unitario 10× abajo es plata mal
descontada en la calle, sin que ningún tablero lo note.

## B — la cola de OC pegaba el estado WMS de otro Centro de Operación

`routes/siesa.py` cruzaba las OC de Siesa contra `RecepcionMercancia` por
`numero_oc_siesa` **sin CO**, mientras su gemela `tienda_oc_service.py:85-90`
sí filtra por CO. Una política, dos implementaciones (corolario de la Regla 0)
— y la identidad documental de Siesa es CO + tipo + consecutivo (Regla 18).

El costo: una tienda recibe su OC `EO1234` en el CO 004 y la OC `EO1234` del
CDI (CO 003) aparece en el muelle como **ya recepcionada**. Nadie la abre, la
mercancía entra al patio y no entra ni al WMS ni a Siesa. Y no reclama nadie:
la pantalla no muestra un error, muestra un estado.
"""
import uuid

import pytest
from unittest.mock import patch


# ══════════════════════════════════════════════════════════════════════════
# A · valor unitario por (referencia, unidad)
# ══════════════════════════════════════════════════════════════════════════

def _parada_con_un_item(db, almacen, unidad_empaque, cantidad=3,
                        unidad_medida='UND'):
    """Ruta EN_TRANSITO con una parada de un solo ítem.
    Devuelve `(ruta, producto, tarea)`."""
    from app.models.usuario import Usuario
    from app.models.conductor import Conductor
    from app.models.ruta_despacho import RutaDespacho
    from app.models.packing import TareaPacking, ItemPacking
    from app.models.producto import Producto
    from app.models.bulto import Bulto

    suf = uuid.uuid4().hex[:6]
    user = Usuario(email=f'cond_{suf}@test.com', nombre='Conductor Dual',
                   rol='conductor', activo=True)
    user.set_password('test123')
    db.session.add(user)
    db.session.flush()

    conductor = Conductor(usuario_id=user.id, nombre='Conductor Dual',
                          cedula=f'99{suf}', activo=True)
    db.session.add(conductor)
    db.session.flush()

    ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana',
                        estado='EN_TRANSITO')
    db.session.add(ruta)
    db.session.flush()

    tarea = TareaPacking(
        codigo=f'PK-DUAL-{suf}', estado='DESPACHADO', almacen_id=almacen.id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1700,
        numero_pedido_siesa=f'PED-{suf}',
    )
    db.session.add(tarea)
    db.session.flush()

    producto = Producto(codigo=f'PAPELSP{suf}', nombre='Papel doble unidad',
                        unidad_empaque=unidad_empaque)
    db.session.add(producto)
    db.session.flush()
    # `unidad_medida` trae default 'UND' en el modelo — se fija después del
    # flush para poder ejercer el caso en que el WMS no declara ninguna unidad.
    producto.unidad_medida = unidad_medida
    db.session.flush()

    db.session.add(ItemPacking(tarea_id=tarea.id, producto_id=producto.id,
                               cantidad_esperada=cantidad, cantidad_real=cantidad))
    db.session.add(Bulto(tarea_id=tarea.id, codigo_barras=f'DUAL-{suf}',
                         tipo='Caja', numero=1, total=1, estado='CARGADO',
                         ruta_despacho_id=ruta.id))
    db.session.commit()
    return ruta, producto, tarea


def _listar(ruta, lineas):
    """Corre `listar_paradas` con Siesa devolviendo `lineas` para la FE."""
    with patch('app.services.fe_resolver.resolver_fe_o_none',
               return_value=('FEW', '1700')), \
         patch('app.services.connekta_gateway.connekta.get_rowids_factura',
               return_value=lineas), \
         patch('app.services.connekta_gateway.connekta.get_pedido_cabecera',
               return_value={'f430_id_cond_pago': 'C01'}), \
         patch('app.services.connekta_gateway.connekta.cond_pago_ventas', 'C01'):
        from app.services.ruta_service import RutaService
        return RutaService.listar_paradas(ruta.id)


# Producto de doble unidad: 3 PQ a 24.250 + 1 UND a 2.425 = 75.175
def _lineas_dual(ref):
    return [
        {'f120_referencia': ref, 'f470_id_unidad_medida': 'PQ',
         'f470_cant_base': 3, 'f470_vlr_neto': 72750, 'f470_rowid': 1},
        {'f120_referencia': ref, 'f470_id_unidad_medida': 'UND',
         'f470_cant_base': 1, 'f470_vlr_neto': 2425, 'f470_rowid': 2},
    ]


class TestElValorUnitarioNoColapsa:
    """El defecto: la última línea gana."""

    def test_dual_unit_da_el_unitario_de_la_linea_del_empaque(self, app, db, almacen):
        """PAPELSP6741: el conductor entrega PQ y devuelve PQ. El unitario que
        le llega a `rutas.js:2199` tiene que ser el de la línea PQ (24.250),
        no el de la línea UND (2.425) que quedaba por ser la última."""
        ruta, producto, _ = _parada_con_un_item(db, almacen, unidad_empaque='PQ')
        parada = _listar(ruta, _lineas_dual(producto.codigo))['paradas'][0]

        assert parada['valor_factura'] == 75175, (
            'el total siempre sumó bien — esa es justamente la asimetría que '
            'hace invisible el defecto del unitario')
        assert parada['items'][0]['valor_unitario'] == 24250, (
            f"unitario colapsado a {parada['items'][0]['valor_unitario']}: la "
            'línea UND pisó a la de PQ. Factor 10 de descuento mal calculado '
            'en la puerta del cliente')

    def test_dual_unit_con_producto_que_se_vende_por_unidad(self, app, db, almacen):
        """La otra mitad de la desambiguación: si el WMS maneja el producto en
        UND, el unitario correcto es el de la línea UND. La clave no es
        «la línea más grande», es **la que coincide con la unidad del ítem**."""
        ruta, producto, _ = _parada_con_un_item(db, almacen, unidad_empaque='UND')
        parada = _listar(ruta, _lineas_dual(producto.codigo))['paradas'][0]
        assert parada['items'][0]['valor_unitario'] == 2425

    def test_sin_empaque_el_UND_no_alcanza_para_desempatar(self, app, db, almacen):
        """**Este test afirmaba 2425 y se cambió a conciencia el 2026-08-20.**

        Decía que sin `unidad_empaque` manda `unidad_medida`, «la misma cadena
        declarada que usa `despacho_parcial_service`». Ese supuesto lo invalidó
        un hallazgo posterior: `siesa_sync_service` leía un campo que **no
        existe en el contrato** (`f120_id_unidad_medida_inventario`; el real es
        `f120_id_unidad_inventario`), así que **todo el catálogo nació con
        `unidad_medida='UND'`** y no se hizo backfill.

        Entonces ese `'UND'` no es una cadena declarada: es el default del sync
        roto, indistinguible de «nadie dijo nada». Usarlo para desempatar es
        elegir en silencio con la evidencia equivocada — y elegir mal cuesta un
        **factor 10** en el precio que el conductor multiplica en la puerta.

        Cuando el sync corra completo y el maestro traiga unidades reales, un
        `unidad_medida` distinto de 'UND' sí desempata: eso lo cubre
        `test_la_unidad_de_medida_real_si_desempata`.
        """
        ruta, producto, _ = _parada_con_un_item(db, almacen, unidad_empaque=None,
                                                unidad_medida='UND')
        parada = _listar(ruta, _lineas_dual(producto.codigo))['paradas'][0]
        assert parada['items'][0]['valor_unitario'] is None, (
            'inventó un unitario a partir del default del sync roto')
        assert parada['items'][0]['valor_unitario_ambiguo'] is True

    def test_sin_ninguna_unidad_no_se_inventa_un_unitario(self, app, db, almacen):
        """**Este test afirmaba 24250 y se cambió a conciencia el 2026-08-20.**

        Heredaba de `despacho_parcial_service` el criterio «mayor cantidad =
        línea principal». Allá elige un `rowid` contra el cual comprometer, y
        equivocarse se corrige. Acá pone un **precio que se multiplica en la
        calle**, y el ítem se muestra en pantalla **sin unidad** — `listar_paradas`
        rotula `items[].unidad` desde el mismo campo vacío.

        Un precio por unidad desconocida × una cantidad de unidad desconocida
        no es una estimación con sesgo: es un número sin dimensión. Elegir «la
        línea más grande» seguía siendo elegir en silencio, solo que distinto.

        Regla 0: ante dato ausente, conservador **y declarado**. La parada cae a
        `modo_pago: LIBRE` —un camino que ya existe y ya está probado— y el
        conductor escribe el monto. Se pierde una comodidad, no la entrega.
        """
        ruta, producto, _ = _parada_con_un_item(db, almacen, unidad_empaque=None,
                                                unidad_medida=None)
        parada = _listar(ruta, _lineas_dual(producto.codigo))['paradas'][0]
        assert parada['items'][0]['valor_unitario'] is None
        assert parada['items'][0]['valor_unitario_ambiguo'] is True

    def test_la_unidad_de_medida_real_si_desempata(self, app, db, almacen):
        """La otra dirección de los dos de arriba: el arreglo no apagó el
        desempate, apagó **una evidencia falsa**. Un `unidad_medida` que el sync
        roto no pudo haber escrito ('PQ') sigue mandando, sin `unidad_empaque`."""
        ruta, producto, _ = _parada_con_un_item(db, almacen, unidad_empaque=None,
                                                unidad_medida='PQ')
        parada = _listar(ruta, _lineas_dual(producto.codigo))['paradas'][0]
        assert parada['items'][0]['valor_unitario'] == 24250
        assert parada['items'][0]['valor_unitario_ambiguo'] is False


class TestElDetectorNoDisparaSobreOperacionSana:
    """Detector en las dos direcciones: un producto de unidad única tiene que
    dar exactamente el mismo valor que daba antes del arreglo."""

    def test_unidad_unica_da_el_mismo_unitario_de_siempre(self, app, db, almacen):
        ruta, producto, _ = _parada_con_un_item(db, almacen, unidad_empaque='UND',
                                                cantidad=5)
        parada = _listar(ruta, [
            {'f120_referencia': producto.codigo, 'f470_id_unidad_medida': 'UND',
             'f470_cant_base': 5, 'f470_vlr_neto': 50000, 'f470_rowid': 1},
        ])['paradas'][0]
        assert parada['valor_factura'] == 50000
        assert parada['items'][0]['valor_unitario'] == 10000
        assert parada['modo_pago'] == 'DINAMICO'

    def test_linea_sin_unidad_de_medida_sigue_resolviendo(self, app, db, almacen):
        """Siesa puede no traer `f470_id_unidad_medida` (el campo no está
        garantizado en toda respuesta). Una sola línea sigue resolviendo: el
        arreglo no puede exigir un campo que antes no se leía."""
        ruta, producto, _ = _parada_con_un_item(db, almacen, unidad_empaque='PQ',
                                                cantidad=5)
        parada = _listar(ruta, [
            {'f120_referencia': producto.codigo,
             'f470_cant_base': 5, 'f470_vlr_neto': 50000},
        ])['paradas'][0]
        assert parada['items'][0]['valor_unitario'] == 10000

    def test_dos_referencias_distintas_no_se_mezclan(self, app, db, almacen):
        """Un arreglo que agrupara de más también sería un defecto."""
        from app.models.packing import ItemPacking
        from app.models.producto import Producto

        ruta, producto, tarea = _parada_con_un_item(db, almacen,
                                                    unidad_empaque='UND',
                                                    cantidad=5)
        otro = Producto(codigo=f'OTRO-{uuid.uuid4().hex[:6]}', nombre='Otro',
                        unidad_empaque='UND')
        db.session.add(otro)
        db.session.flush()
        db.session.add(ItemPacking(tarea_id=tarea.id, producto_id=otro.id,
                                   cantidad_esperada=2, cantidad_real=2))
        db.session.commit()

        parada = _listar(ruta, [
            {'f120_referencia': producto.codigo, 'f470_id_unidad_medida': 'UND',
             'f470_cant_base': 5, 'f470_vlr_neto': 50000},
            {'f120_referencia': otro.codigo, 'f470_id_unidad_medida': 'UND',
             'f470_cant_base': 2, 'f470_vlr_neto': 300},
        ])['paradas'][0]
        por_codigo = {i['codigo']: i['valor_unitario'] for i in parada['items']}
        assert por_codigo[producto.codigo] == 10000
        assert por_codigo[otro.codigo] == 150


# ══════════════════════════════════════════════════════════════════════════
# B · la cola de OC y el Centro de Operación
# ══════════════════════════════════════════════════════════════════════════

NUMERO_OC = 'EO9911'


@pytest.fixture
def fila_oc():
    """Una línea de OC de Siesa en el CO/bodega del CDI."""
    from app.services.connekta_gateway import connekta
    return {
        'f420_id_co': connekta.centro_op,
        'f150_id': connekta.bodega,
        'f420_id_tipo_docto': 'EO',
        'f420_consec_docto': '9911',
        'f421_cant_pedida': 10,
        'f421_cant_entrada': 0,
        'f120_referencia': 'REF-OC-1',
        'f120_descripcion': 'Producto de prueba',
        'f200_razon_social_prov': 'Proveedor SA',
        'f200_nit_prov': '900123456',
        'f421_factor': 1,
    }


def _recepcion(db, almacen, co, estado='CONFIRMADA'):
    from app.models.recepcion import RecepcionMercancia
    r = RecepcionMercancia(
        codigo=f'REC-{uuid.uuid4().hex[:8]}',
        numero_oc_siesa=NUMERO_OC, co_oc_siesa=co,
        tipo_docto_oc_siesa='EO', consec_docto_oc_siesa='9911',
        almacen_id=almacen.id, estado=estado,
    )
    db.session.add(r)
    db.session.commit()
    return r


def _cola(client, jwt_token_admin, fila_oc):
    with patch('app.routes.siesa.connekta.get_ordenes_compra_aprobadas',
               return_value={'detalle': {'Table': [fila_oc]}}):
        resp = client.get('/api/siesa/ordenes-compra',
                          headers={'Authorization': f'Bearer {jwt_token_admin}'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()['ordenes']


class TestLaColaDeOCSeIndexaConSuCentroDeOperacion:

    def test_recepcion_de_otro_co_no_marca_la_oc_del_cdi(
            self, client, db, almacen, jwt_token_admin, fila_oc):
        """El defecto: la tienda (CO 004) recibió *su* EO9911 y el muelle del
        CDI (CO 003) muestra la suya como ya recepcionada."""
        _recepcion(db, almacen, co='004', estado='CONFIRMADA')
        ordenes = _cola(client, jwt_token_admin, fila_oc)
        assert len(ordenes) == 1
        assert ordenes[0]['recepcion_wms_estado'] is None, (
            'el estado WMS de otro Centro de Operación se pegó a esta OC — '
            'la mercancía del CDI no la va a abrir nadie')


class TestNoDisparaSobreOperacionSana:
    """La otra dirección: una OC de un solo CO se agrupa y se marca igual."""

    def test_recepcion_del_mismo_co_si_marca_la_oc(
            self, client, db, almacen, jwt_token_admin, fila_oc):
        from app.services.connekta_gateway import connekta
        _recepcion(db, almacen, co=connekta.centro_op, estado='EN_PROCESO')
        ordenes = _cola(client, jwt_token_admin, fila_oc)
        assert ordenes[0]['recepcion_wms_estado'] == 'EN_PROCESO'

    def test_sin_recepcion_no_hay_estado(
            self, client, db, almacen, jwt_token_admin, fila_oc):
        ordenes = _cola(client, jwt_token_admin, fila_oc)
        assert len(ordenes) == 1
        assert ordenes[0]['numero_oc'] == NUMERO_OC
        assert ordenes[0]['co'] == fila_oc['f420_id_co']
        assert ordenes[0]['recepcion_wms_estado'] is None
        assert ordenes[0]['items'][0]['cantidad_pendiente'] == 10

    def test_recepcion_vieja_sin_co_sigue_marcando(
            self, client, db, almacen, jwt_token_admin, fila_oc):
        """`co_oc_siesa` es nullable y las filas anteriores a
        `3f9a1c7d2e85` pueden no tenerlo. Ignorarlas mostraría como pendiente
        una OC ya recibida y mandaría a alguien a recibirla dos veces — Regla
        0: ante dato ausente, el lado conservador es **no** perder el estado."""
        _recepcion(db, almacen, co='', estado='CONFIRMADA')
        ordenes = _cola(client, jwt_token_admin, fila_oc)
        assert ordenes[0]['recepcion_wms_estado'] == 'CONFIRMADA'

    def test_cancelada_no_cuenta(
            self, client, db, almacen, jwt_token_admin, fila_oc):
        from app.services.connekta_gateway import connekta
        _recepcion(db, almacen, co=connekta.centro_op, estado='CANCELADA')
        ordenes = _cola(client, jwt_token_admin, fila_oc)
        assert ordenes[0]['recepcion_wms_estado'] is None


class TestIniciarRecepcionNoConfundeCentrosDeOperacion:
    """La misma clave incompleta, del lado que **actúa**: la guarda de
    idempotencia de `POST /api/siesa/iniciar-recepcion` buscaba la recepción
    existente solo por `numero_oc_siesa`. La OC de la tienda (otro CO) devolvía
    409 «ya fue recepcionada» sobre la OC del CDI — mercancía en el patio que
    el recepcionista no puede ni empezar a recibir, y nada que lo explique.

    `recepcion_service.crear_recepcion` ya incluía el CO en su filtro
    (`recepcion_service.py:42-43`): la ruta era el sitio que había quedado
    corto. Una política, una función."""

    def _payload(self, almacen, producto, co):
        return {
            'numero_oc': NUMERO_OC, 'tipo_docto': 'EO', 'consec_docto': '9911',
            'almacen_id': almacen.id, 'co': co,
            'items': [{'producto_id': producto.id, 'cantidad_ordenada': 10}],
        }

    def _post(self, client, jwt_token_admin, payload):
        return client.post('/api/siesa/iniciar-recepcion', json=payload,
                           headers={'Authorization': f'Bearer {jwt_token_admin}'})

    def test_la_oc_de_otro_co_no_bloquea(self, client, db, almacen, producto,
                                          jwt_token_admin):
        from app.services.connekta_gateway import connekta
        _recepcion(db, almacen, co='004', estado='CONFIRMADA')
        resp = self._post(client, jwt_token_admin,
                          self._payload(almacen, producto, connekta.centro_op))
        assert resp.status_code == 201, resp.get_data(as_text=True)

    def test_la_misma_oc_del_mismo_co_si_bloquea(self, client, db, almacen,
                                                  producto, jwt_token_admin):
        """La otra dirección: el guard tiene que seguir atrapando el duplicado
        real — dos recepciones activas sobre la misma OC son dos entradas 142948
        con doble suma de inventario."""
        from app.services.connekta_gateway import connekta
        _recepcion(db, almacen, co=connekta.centro_op, estado='CONFIRMADA')
        resp = self._post(client, jwt_token_admin,
                          self._payload(almacen, producto, connekta.centro_op))
        assert resp.status_code == 409

    def test_la_recepcion_vieja_sin_co_sigue_bloqueando(
            self, client, db, almacen, producto, jwt_token_admin):
        """Regla 0: `co_oc_siesa` vacío no dice de quién es. Duplicar una
        entrada de inventario es irreversible desde el WMS; bloquear de más se
        resuelve cancelando la recepción desde Admin."""
        from app.services.connekta_gateway import connekta
        _recepcion(db, almacen, co='', estado='CONFIRMADA')
        resp = self._post(client, jwt_token_admin,
                          self._payload(almacen, producto, connekta.centro_op))
        assert resp.status_code == 409
