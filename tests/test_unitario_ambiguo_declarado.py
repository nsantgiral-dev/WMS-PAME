"""
El desempate del valor unitario se apaga en silencio justo donde hacía falta.

`ruta_service._valor_y_cond_pago` calcula `{referencia: valor_neto_unitario}`
y `listar_paradas` lo cuelga de cada ítem como `valor_unitario`. Ese número
viaja a `rutas.js:2199`, donde el conductor multiplica lo devuelto en la puerta
del cliente. Un factor de empaque de error ahí es plata mal descontada en la
calle, y **ningún tablero lo nota**: `valor_factura` suma bien igual.

El arreglo anterior desempató las dos líneas de un producto de doble unidad
(PQ + UND, PAPELSP6741) comparando `f470_id_unidad_medida` contra la unidad del
producto en el WMS. Dos agujeros medidos:

1. **Degrada al defecto original con el maestro incompleto.** La unidad del WMS
   se tomaba de `unidad_empaque` y, si estaba vacía, de `unidad_medida` — cuyo
   default de modelo es `'UND'`. Y `siesa_sync_service.py:19-22` documenta que
   **todo el catálogo nació con `unidad_medida='UND'`** por leer un campo que
   el contrato no declara, sin backfill. O sea: para la mayoría del catálogo
   ese `'UND'` no es una declaración, es el default. Con él ganaba otra vez la
   línea UND — exactamente el defecto que el arreglo dice cerrar.

2. **El orden de las filas decidía.** Con `>` estricto sobre el puntaje, ante
   empate ganaba la primera fila que llegó. Un auditor lo ejecutó: 24250.0 y
   2425.0 con las mismas dos líneas en orden invertido. Eso no es un desempate,
   es el orden en que la API devolvió los datos.

Lo que se exige acá (Regla 0 — ante dato ausente, fallar hacia el lado
conservador **y declararlo**): cuando el WMS no declara con qué unidad maneja
un producto que la factura cobra en más de una unidad, **el unitario no existe**
y hay que decirlo, no inventarlo. Ver el docstring de `_unitarios_por_referencia`
para el porqué de "None" en vez de "el de la línea más grande".
"""
import uuid

from unittest.mock import patch


# ── armado ──────────────────────────────────────────────────────────────────

def _parada(db, almacen, unidad_empaque, unidad_medida='UND', cantidad=3):
    """Ruta EN_TRANSITO con una parada de un solo ítem. `(ruta, producto)`."""
    from app.models.usuario import Usuario
    from app.models.conductor import Conductor
    from app.models.ruta_despacho import RutaDespacho
    from app.models.packing import TareaPacking, ItemPacking
    from app.models.producto import Producto
    from app.models.bulto import Bulto

    suf = uuid.uuid4().hex[:6]
    user = Usuario(email=f'amb_{suf}@test.com', nombre='Conductor Ambiguo',
                   rol='conductor', activo=True)
    user.set_password('test123')
    db.session.add(user)
    db.session.flush()

    conductor = Conductor(usuario_id=user.id, nombre='Conductor Ambiguo',
                          cedula=f'88{suf}', activo=True)
    db.session.add(conductor)
    db.session.flush()

    ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana',
                        estado='EN_TRANSITO')
    db.session.add(ruta)
    db.session.flush()

    tarea = TareaPacking(
        codigo=f'PK-AMB-{suf}', estado='DESPACHADO', almacen_id=almacen.id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1800,
        numero_pedido_siesa=f'PED-{suf}',
    )
    db.session.add(tarea)
    db.session.flush()

    producto = Producto(codigo=f'PAPELSP{suf}', nombre='Papel doble unidad',
                        unidad_empaque=unidad_empaque)
    db.session.add(producto)
    db.session.flush()
    # `unidad_medida` trae default 'UND' en el modelo: se fija DESPUÉS del
    # flush para poder ejercer también el caso sin ninguna unidad declarada.
    producto.unidad_medida = unidad_medida
    db.session.flush()

    db.session.add(ItemPacking(tarea_id=tarea.id, producto_id=producto.id,
                               cantidad_esperada=cantidad, cantidad_real=cantidad))
    db.session.add(Bulto(tarea_id=tarea.id, codigo_barras=f'AMB-{suf}',
                         tipo='Caja', numero=1, total=1, estado='CARGADO',
                         ruta_despacho_id=ruta.id))
    db.session.commit()
    return ruta, producto


def _listar(ruta, lineas):
    with patch('app.services.fe_resolver.resolver_fe_o_none',
               return_value=('FEW', '1800')), \
         patch('app.services.connekta_gateway.connekta.get_rowids_factura',
               return_value=lineas), \
         patch('app.services.connekta_gateway.connekta.get_pedido_cabecera',
               return_value={'f430_id_cond_pago': 'C01'}), \
         patch('app.services.connekta_gateway.connekta.cond_pago_ventas', 'C01'):
        from app.services.ruta_service import RutaService
        return RutaService.listar_paradas(ruta.id)


def _lineas_dual(ref, cant_pq=3, cant_und=1):
    """PAPELSP6741 en la FE: 3 PQ a 24.250 + 1 UND a 2.425 = 75.175."""
    return [
        {'f120_referencia': ref, 'f470_id_unidad_medida': 'PQ',
         'f470_cant_base': cant_pq, 'f470_vlr_neto': 24250 * cant_pq,
         'f470_rowid': 1},
        {'f120_referencia': ref, 'f470_id_unidad_medida': 'UND',
         'f470_cant_base': cant_und, 'f470_vlr_neto': 2425 * cant_und,
         'f470_rowid': 2},
    ]


# ══════════════════════════════════════════════════════════════════════════
# 1 · Reproducción — hoy elige en silencio
# ══════════════════════════════════════════════════════════════════════════

class TestNoInventaUnUnitarioQueNoPuedeSaber:

    def test_maestro_incompleto_no_puede_dar_un_unitario(self, app, db, almacen):
        """`unidad_empaque` vacío + `unidad_medida='UND'` (el default que dejó
        el sync roto en todo el catálogo) sobre un producto que la factura
        cobra en PQ **y** en UND.

        Hoy: gana la línea UND y el conductor ve 2.425 — el defecto original,
        con el arreglo puesto. Y la pantalla ni siquiera puede nombrar la
        unidad del ítem (`items[].unidad` sale del mismo `unidad_empaque`
        vacío), así que el número que se muestra es un precio por una unidad
        que nadie sabe cuál es.

        Lo que se exige: `valor_unitario is None` y la parada declarada como
        no desambiguada. El frontend ya sabe caer al campo libre cuando falta
        el valor — es el mismo camino que usa `es_contado = None`.
        """
        ruta, producto = _parada(db, almacen, unidad_empaque=None,
                                 unidad_medida='UND')
        d = _listar(ruta, _lineas_dual(producto.codigo))
        parada = d['paradas'][0]

        assert parada['valor_factura'] == 75175, (
            'el total siempre sumó bien — romperlo sería la peor regresión')
        assert parada['items'][0]['valor_unitario'] is None, (
            f"unitario inventado: {parada['items'][0]['valor_unitario']}. Con "
            "`unidad_empaque` vacío el WMS no declara unidad y el 'UND' de "
            '`unidad_medida` es el default del sync roto, no una declaración')
        assert parada['items'][0]['valor_unitario_ambiguo'] is True
        assert parada['referencias_ambiguas'] == 1
        assert parada['referencias_valoradas'] == 1
        # Un conteo sin su base no es una medición.
        assert d['referencias_ambiguas'] == 1
        assert d['referencias_valoradas'] == 1
        # Sin unitario confiable, la pantalla cae al campo libre de siempre.
        assert parada['modo_pago'] == 'LIBRE'

    def test_el_orden_de_las_filas_no_puede_cambiar_el_unitario(self, app, db, almacen):
        """Ejecutado por un auditor: **24250.0 vs 2425.0 al invertir las filas.**

        Ninguna unidad de la factura coincide con la del WMS ('CJA') y las
        cantidades empatan, así que el `>` estricto dejaba ganar a la primera
        fila que llegó. Un resultado que cambia con el orden en que la API
        devolvió los datos no es un desempate: es azar.
        """
        lineas = _lineas_dual('X', cant_pq=1, cant_und=1)

        ruta_a, prod_a = _parada(db, almacen, unidad_empaque='CJA', cantidad=1)
        la = _lineas_dual(prod_a.codigo, cant_pq=1, cant_und=1)
        directo = _listar(ruta_a, la)['paradas'][0]['items'][0]['valor_unitario']

        ruta_b, prod_b = _parada(db, almacen, unidad_empaque='CJA', cantidad=1)
        lb = list(reversed(_lineas_dual(prod_b.codigo, cant_pq=1, cant_und=1)))
        invertido = _listar(ruta_b, lb)['paradas'][0]['items'][0]['valor_unitario']

        assert directo == invertido, (
            f'el orden de las filas decidió el unitario: {directo} vs '
            f'{invertido} con las mismas dos líneas')
        assert directo is None, (
            f'unitario {directo} elegido sin criterio: ninguna unidad de la '
            'factura coincide con la del WMS y las cantidades empatan')
        assert len(lineas) == 2  # las filas de referencia no se tocaron


# ══════════════════════════════════════════════════════════════════════════
# 2 · Detector en las dos direcciones — no puede disparar sobre lo sano
# ══════════════════════════════════════════════════════════════════════════

class TestNoDisparaSobreOperacionSana:

    def test_unidad_unica_da_exactamente_el_mismo_unitario_de_antes(self, app, db, almacen):
        """Una sola línea: nada que desambiguar, nada que declarar."""
        ruta, producto = _parada(db, almacen, unidad_empaque='UND', cantidad=5)
        d = _listar(ruta, [
            {'f120_referencia': producto.codigo, 'f470_id_unidad_medida': 'UND',
             'f470_cant_base': 5, 'f470_vlr_neto': 50000, 'f470_rowid': 1},
        ])
        parada = d['paradas'][0]
        assert parada['valor_factura'] == 50000
        assert parada['items'][0]['valor_unitario'] == 10000
        assert parada['items'][0]['valor_unitario_ambiguo'] is False
        assert parada['referencias_ambiguas'] == 0
        assert parada['referencias_valoradas'] == 1
        assert parada['modo_pago'] == 'DINAMICO'

    def test_doble_unidad_con_empaque_poblado_sigue_desempatando(self, app, db, almacen):
        """PAPELSP6741 tal cual: `unidad_empaque='PQ'` es una declaración real
        (el sync roto solo tocaba `unidad_medida`). El arreglo anterior sigue
        valiendo: 24.250, no 2.425, y sin declarar ambigüedad."""
        ruta, producto = _parada(db, almacen, unidad_empaque='PQ')
        d = _listar(ruta, _lineas_dual(producto.codigo))
        parada = d['paradas'][0]
        assert parada['valor_factura'] == 75175
        assert parada['items'][0]['valor_unitario'] == 24250
        assert parada['items'][0]['valor_unitario_ambiguo'] is False
        assert parada['referencias_ambiguas'] == 0
        assert parada['modo_pago'] == 'DINAMICO'

    def test_empaque_poblado_en_und_tambien_desempata(self, app, db, almacen):
        """La otra mitad: si el WMS declara que maneja el producto en UND, el
        unitario correcto es el de la línea UND. La clave no es 'la línea más
        grande', es la que coincide con la unidad **declarada**."""
        ruta, producto = _parada(db, almacen, unidad_empaque='UND')
        parada = _listar(ruta, _lineas_dual(producto.codigo))['paradas'][0]
        assert parada['items'][0]['valor_unitario'] == 2425
        assert parada['items'][0]['valor_unitario_ambiguo'] is False

    def test_unidad_medida_distinta_del_default_si_es_una_declaracion(self, app, db, almacen):
        """`unidad_medida='PQ'` no puede venir del sync roto —ese escribía
        'UND' en todo— así que sí es evidencia. Solo el `'UND'` sin
        `unidad_empaque` es indistinguible del default."""
        ruta, producto = _parada(db, almacen, unidad_empaque=None,
                                 unidad_medida='PQ')
        parada = _listar(ruta, _lineas_dual(producto.codigo))['paradas'][0]
        assert parada['items'][0]['valor_unitario'] == 24250
        assert parada['items'][0]['valor_unitario_ambiguo'] is False

    def test_linea_partida_en_la_misma_unidad_no_es_ambigua(self, app, db, almacen):
        """Dos líneas de la misma referencia **en la misma unidad** no tienen
        el riesgo de factor de empaque: el unitario es el mismo en las dos.
        Declararlas ambiguas sería un falso positivo, y un canal de avisos con
        falsos positivos deja de leerse."""
        ruta, producto = _parada(db, almacen, unidad_empaque=None,
                                 unidad_medida='UND', cantidad=5)
        d = _listar(ruta, [
            {'f120_referencia': producto.codigo, 'f470_id_unidad_medida': 'UND',
             'f470_cant_base': 3, 'f470_vlr_neto': 30000, 'f470_rowid': 1},
            {'f120_referencia': producto.codigo, 'f470_id_unidad_medida': 'UND',
             'f470_cant_base': 2, 'f470_vlr_neto': 20000, 'f470_rowid': 2},
        ])
        parada = d['paradas'][0]
        assert parada['valor_factura'] == 50000
        assert parada['items'][0]['valor_unitario'] == 10000
        assert parada['items'][0]['valor_unitario_ambiguo'] is False
        assert parada['referencias_ambiguas'] == 0

    def test_valor_factura_no_cambia_nunca(self, app, db, almacen):
        """`valor_factura` es el dato sano: suma TODAS las líneas, ambiguas o
        no. Que el unitario se declare desconocido no puede tocarlo."""
        ruta, producto = _parada(db, almacen, unidad_empaque=None,
                                 unidad_medida='UND')
        d = _listar(ruta, _lineas_dual(producto.codigo))
        assert d['paradas'][0]['valor_factura'] == 75175
        assert d['paradas'][0]['items'][0]['valor_unitario'] is None

    def test_dos_referencias_una_ambigua_no_contamina_a_la_otra(self, app, db, almacen):
        """El conteo tiene base: 2 referencias, 1 ambigua. Y la referencia sana
        conserva su unitario exacto."""
        from app.models.packing import ItemPacking, TareaPacking
        from app.models.producto import Producto

        ruta, producto = _parada(db, almacen, unidad_empaque=None,
                                 unidad_medida='UND')
        tarea = TareaPacking.query.filter_by(consec_docto_pedido_siesa=1800) \
            .order_by(TareaPacking.id.desc()).first()
        otro = Producto(codigo=f'SANO-{uuid.uuid4().hex[:6]}', nombre='Sano',
                        unidad_empaque='UND')
        db.session.add(otro)
        db.session.flush()
        db.session.add(ItemPacking(tarea_id=tarea.id, producto_id=otro.id,
                                   cantidad_esperada=2, cantidad_real=2))
        db.session.commit()

        d = _listar(ruta, _lineas_dual(producto.codigo) + [
            {'f120_referencia': otro.codigo, 'f470_id_unidad_medida': 'UND',
             'f470_cant_base': 2, 'f470_vlr_neto': 300, 'f470_rowid': 9},
        ])
        parada = d['paradas'][0]
        por_codigo = {i['codigo']: i['valor_unitario'] for i in parada['items']}
        assert por_codigo[otro.codigo] == 150
        assert por_codigo[producto.codigo] is None
        assert parada['referencias_valoradas'] == 2
        assert parada['referencias_ambiguas'] == 1
        # Una sola referencia sin unitario ya obliga al campo libre: el modo
        # DINAMICO exige que TODOS los ítems tengan valor.
        assert parada['modo_pago'] == 'LIBRE'


# ══════════════════════════════════════════════════════════════════════════
# 3 · El desempate como función pura — sin base ni Siesa
# ══════════════════════════════════════════════════════════════════════════

class TestDesempateDeterminista:

    def _f(self):
        from app.services.ruta_service import RutaService
        return RutaService._unitarios_por_referencia

    def test_mismo_resultado_con_las_filas_en_cualquier_orden(self):
        f = self._f()
        lineas = _lineas_dual('A', cant_pq=3, cant_und=1)
        a, da = f(lineas, {'A': 'PQ'})
        b, dbb = f(list(reversed(lineas)), {'A': 'PQ'})
        assert a == b == {'A': 24250.0}
        assert da['ambiguas'] == dbb['ambiguas'] == 0

    def test_empate_total_declara_en_los_dos_ordenes(self):
        f = self._f()
        lineas = _lineas_dual('A', cant_pq=1, cant_und=1)
        a, da = f(lineas, {'A': 'CJA'})
        b, dbb = f(list(reversed(lineas)), {'A': 'CJA'})
        assert a == b == {'A': None}
        assert da['ambiguas'] == dbb['ambiguas'] == 1
        assert da['valoradas'] == 1
        assert set(da['referencias']) == {'A'}

    def test_sin_unidad_en_el_wms_es_ambiguo_aunque_las_cantidades_no_empaten(self):
        """3 PQ contra 1 UND. 'La línea más grande' devolvería 24250, pero el
        WMS no declara en qué unidad cuenta el conductor lo devuelto: ese
        número multiplicaría una cantidad de unidad desconocida."""
        f = self._f()
        valores, diag = f(_lineas_dual('A'), {})
        assert valores == {'A': None}
        assert diag['ambiguas'] == 1

    def test_tarea_sin_fe_devuelve_cuatro_elementos(self, app, db, almacen):
        """Encontrado de paso, en el camino de salida temprana: cuando no hay
        FE resoluble, `_valor_y_cond_pago` devolvía una tupla de **3** mientras
        sus dos callers desempaquetan **4**. No es un valor mal calculado, es
        un `ValueError` que se lleva la lista de paradas entera — y le toca
        justo a la tarea sin factura, que es el caso que el `None` de
        `es_contado` existe para atender sin romper nada."""
        from app.services.ruta_service import RutaService

        with patch('app.services.fe_resolver.resolver_fe_o_none',
                   return_value=(None, None)):
            valor, contado, valores, crudo = \
                RutaService._valor_y_cond_pago(object())
        assert (valor, contado, valores, crudo) == (None, None, {}, None)

    def test_lineas_sin_cantidad_no_entran_ni_al_conteo(self):
        f = self._f()
        valores, diag = f([
            {'f120_referencia': 'A', 'f470_cant_base': 0, 'f470_vlr_neto': 100},
        ], {})
        assert valores == {}
        assert diag['valoradas'] == 0 and diag['ambiguas'] == 0
