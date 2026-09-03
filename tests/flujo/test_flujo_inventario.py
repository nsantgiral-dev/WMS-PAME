"""
El libro de movimientos, en las dos direcciones.

`INV-01`/`INV-02` cuadran `MovimientoInventario` contra `UbicacionProducto`. Un
guard así se cae de un lado o del otro, y hay que probar los dos:

    · **que NO dispara** sobre una cadena sana — si disparara, el canal se
      llenaría de ruido y se aprendería a ignorarlo (los 639 avisos conocidos);
    · **que SÍ dispara** cuando alguien mueve el stock sin escribir el libro —
      si no disparara, «0 hallazgos» no significaría nada.

Los seis guards del 2026-08-15 tenían el primero. Ninguno tenía el segundo.

## La cadena sana se construye con el servicio real

`PickingService.confirmar_picking`, no `MovimientoInventario(...)`. Un arnés que
escribe las filas del libro a mano prueba que la base las acepta, que es
exactamente lo que ya hacen los ~1900 tests y por eso ninguno ve un defecto de
frontera.

## Y la violación también

`test_ve_una_cantidad_movida_sin_movimiento` no escribe el descuadre: **corre el
job `AJUSTE_CONTEO` de verdad** (`siesa_job_service._ejecutar_job`), que muta
`UbicacionProducto.cantidad` en su rama de recuperación sin escribir ningún
`MovimientoInventario`. Si mañana alguien le agrega el movimiento que le falta,
este test se pone rojo — y eso es correcto: el detector deja de tener qué ver
porque el defecto se arregló, y hay que venir a leer esto antes de tocarlo.
"""
import json
from datetime import datetime

import pytest

from app.services import auditoria


def _res(codigo):
    r = auditoria.auditar('inventario')
    return next(x for x in r['resultados'] if x['codigo'] == codigo)


@pytest.fixture
def kardex(db, almacen, usuario):
    """Un bin con **dos** movimientos reales encima.

    Dos y no uno: el encadenamiento necesita un eslabón anterior contra el cual
    comparar. Con un solo movimiento `INV-02` pasaría por vacío, que es la forma
    de un guard que no puede fallar.
    """
    from app.models.inventario import UbicacionProducto
    from app.services.picking_service import PickingService
    from tests.flujo.conductor_de_flujo import sembrar_catalogo

    productos, ub = sembrar_catalogo(db, almacen, n=1, con_stock=50)
    prod = productos[0]
    for recoge in (7, 5):
        for t in PickingService.crear_tareas(
                producto_id=prod.id, cantidad=recoge, almacen_id=almacen.id,
                referencia_documento=f'PED-KDX-{recoge}', tipo_documento='PEDIDO'):
            PickingService.iniciar_picking(t.id, usuario.id)
            PickingService.confirmar_picking(t.id, recoge, usuario.id)
    db.session.commit()

    reg = UbicacionProducto.query.filter_by(
        ubicacion_id=ub.id, producto_id=prod.id).first()

    class _Kardex:
        ubicacion = ub
        producto = prod
        registro = reg
        almacen_id = almacen.id
    return _Kardex


class TestElDetectorNoDisparaSobreUnaCadenaSana:
    """La otra mitad. Un detector que grita sobre operación normal no informa:
    entrena a que lo ignoren."""

    def test_dos_picking_seguidos_no_producen_hallazgos(self, db, kardex):
        assert _res('INV-01')['total'] == 0
        assert _res('INV-02')['total'] == 0
        assert _res('INV-03')['total'] == 0

    def test_el_saldo_del_libro_es_el_del_estante(self, db, kardex):
        """Lo que hace que la comparación signifique algo: los dos números
        existen y se movieron de verdad (50 → 43 → 38)."""
        from app.models.inventario import MovimientoInventario
        movs = (MovimientoInventario.query
                .filter_by(ubicacion_id=kardex.ubicacion.id)
                .order_by(MovimientoInventario.id).all())
        assert [m.saldo_antes for m in movs] == [50, 43]
        assert [m.saldo_despues for m in movs] == [43, 38]
        assert kardex.registro.cantidad == 38

    def test_ninguno_revienta(self, db, kardex):
        assert not auditoria.auditar('inventario')['errores']

    def test_un_movimiento_sin_saldos_no_acusa_a_nadie(self, db, kardex):
        """Cinco escritores guardan el movimiento con los saldos en NULL
        (reposición, short-pick, las tres ramas de auditoría de picking).

        Tratar ese NULL como cero fabricaría un descuadre por cada uno de ellos
        — Regla 0: el dato ausente se declara, no se rellena. La clave queda
        ciega y `INV-09` la cuenta.
        """
        from app.models.inventario import MovimientoInventario
        db.session.add(MovimientoInventario(
            producto_id=kardex.producto.id, ubicacion_id=kardex.ubicacion.id,
            almacen_id=kardex.almacen_id, tipo='REPOSICION', cantidad=4,
            motivo='movimiento sin saldos, como los escribe reposicion_service'))
        kardex.registro.cantidad += 4
        db.session.commit()

        assert _res('INV-01')['total'] == 0
        assert _res('INV-02')['total'] == 0
        assert _res('INV-09')['hallazgos'][0]['datos']['claves_ciegas'] == 1

    def test_una_clave_con_dos_lotes_no_se_compara(self, db, kardex):
        """`ubicaciones_productos` es única por `(ubicacion, producto, lote)` y
        todos los escritores buscan la suya con `.first()`, sin lote. El
        `saldo_despues` guardado es el de **una** fila; compararlo contra la
        suma acusaría a quien no hizo nada."""
        from app.models.inventario import UbicacionProducto
        db.session.add(UbicacionProducto(
            ubicacion_id=kardex.ubicacion.id, producto_id=kardex.producto.id,
            cantidad=11, lote='L-2', reservado=0, bloqueado=0))
        db.session.commit()

        assert _res('INV-01')['total'] == 0
        assert _res('INV-09')['hallazgos'][0]['datos']['claves_multilote'] == 1

    def test_fuera_de_la_ventana_no_se_mira(self, db, kardex):
        """Y por eso la ventana se declara: lo que quedó fuera no se auditó, no
        es que estuviera bien."""
        from datetime import timedelta

        from app.models.inventario import MovimientoInventario
        for m in MovimientoInventario.query.all():
            m.fecha = datetime.utcnow() - timedelta(days=400)
        kardex.registro.cantidad = 999
        db.session.commit()

        assert _res('INV-01')['total'] == 0
        assert _res('INV-09')['total'] == 0


class TestElDetectorNoEstaCiego:
    """Se rompe el libro a propósito y se exige que el invariante lo vea."""

    def test_ve_una_cantidad_movida_sin_movimiento(self, db, kardex, usuario):
        """**La violación la escribe el código roto, no el test.**

        `siesa_job_service`, rama de recuperación de `AJUSTE_CONTEO`, muta
        `UbicacionProducto.cantidad` y **no escribe ningún
        `MovimientoInventario`** — ni ahí ni en la rama principal del mismo job.
        El stock del WMS cambia y el libro no se entera.

        Y es la asimetría que hace que este guard pueda fallar: el camino roto
        no toca `movimientos_inventario`, así que **no puede mover el último
        `saldo_despues` para taparse**.
        """
        from app.models.conteo import EstadoConteo, SesionConteo
        from app.models.inventario import MovimientoInventario
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _ejecutar_job

        sesion = SesionConteo(
            codigo='CNT-INV01', tipo='MANUAL', ubicacion_id=kardex.ubicacion.id,
            almacen_id=kardex.almacen_id, producto_id=kardex.producto.id,
            estado=EstadoConteo.AJUSTANDO, siesa_triggered=True,
            operario_id=usuario.id)
        db.session.add(sesion)
        db.session.flush()
        job = SiesaJob(tipo='AJUSTE_CONTEO', payload=json.dumps({
            'sesion_id': sesion.id, 'ubicacion_id': kardex.ubicacion.id,
            'producto_id': kardex.producto.id, 'cantidad': 6,
            'motivo_codigo': 'AJ-SAL'}))
        db.session.add(job)
        db.session.commit()

        movs_antes = MovimientoInventario.query.count()
        _ejecutar_job(job)

        # El job movió el stock y no dejó rastro en el libro: las dos mitades
        # del defecto, verificadas antes de mirar al auditor.
        assert kardex.registro.cantidad == 32
        assert MovimientoInventario.query.count() == movs_antes

        r = _res('INV-01')
        assert r['total'] == 1, 'INV-01 no vio un ajuste que movió stock sin libro'
        assert r['hallazgos'][0]['datos']['diferencia'] == -6
        assert r['hallazgos'][0]['datos']['saldo_despues'] == 38
        assert r['hallazgos'][0]['datos']['cantidad_actual'] == 32

    def test_ve_la_cadena_rota_entre_dos_movimientos(self, db, kardex):
        """El escritor fantasma de la mitad de la cadena.

        Alguien movió el bin entre el movimiento 1 y el 2 sin escribir nada: el
        2 abrió en un saldo que el 1 no dejó. Es el mismo defecto que `INV-01`,
        visto **hacia atrás** — y se ve aunque el último movimiento haya vuelto
        a cuadrar el saldo vivo, que es cuando `INV-01` ya no puede verlo.
        """
        from app.models.inventario import MovimientoInventario
        movs = (MovimientoInventario.query
                .filter_by(ubicacion_id=kardex.ubicacion.id)
                .order_by(MovimientoInventario.id).all())
        movs[0].saldo_despues = 40      # cerró en 40 y el siguiente abrió en 43
        db.session.commit()

        r = _res('INV-02')
        assert r['total'] == 1, 'INV-02 no vio el salto en la cadena'
        assert r['hallazgos'][0]['datos']['diferencia'] == 3
        assert r['hallazgos'][0]['datos']['primer_salto'] == [movs[0].id, movs[1].id]
        # INV-01 sigue en cero: el último eslabón cuadra con el estante. Por eso
        # las dos propiedades son dos y no una.
        assert _res('INV-01')['total'] == 0

    def test_ve_un_movimiento_firmado_contra_otro_almacen(self, db, kardex):
        """El descuento cruzado de bodega: el movimiento nombra un bin del CD y
        se firma contra la tienda (o al revés).

        Los dos valores vienen de **fuentes distintas** —el maestro de
        ubicaciones y la operación que firma—, así que ninguna vía sana los hace
        coincidir por construcción. Por eso este sí bloquea.
        """
        from app.models.almacen import Almacen
        from app.models.inventario import MovimientoInventario

        otro = Almacen(codigo='ALM-NC1', nombre='Neiva Centro',
                       bodega_siesa_id='NC1', activo=True)
        db.session.add(otro)
        db.session.flush()
        mov = (MovimientoInventario.query
               .filter_by(ubicacion_id=kardex.ubicacion.id)
               .order_by(MovimientoInventario.id.desc()).first())
        mov.almacen_id = otro.id
        db.session.commit()

        r = _res('INV-03')
        assert r['total'] == 1, 'INV-03 no vio el movimiento firmado contra otra bodega'
        assert r['hallazgos'][0]['datos']['almacen_del_bin'] == kardex.almacen_id
        assert r['hallazgos'][0]['datos']['almacen_firmado'] == otro.id
        assert r['severidad'] == auditoria.BLOQUEA

    def test_ve_el_descuento_que_cruza_de_bodega(self, db, kardex, almacen,
                                                 usuario):
        """El defecto de `traslado_service`: descontar de bins de **otro**
        almacén y firmar el movimiento contra el propio.

        Su movimiento va **sin `ubicacion_id`** (es agregado por almacén), así
        que no cae en ninguna cadena y ni `INV-02` ni `INV-03` lo alcanzan. Se
        ve por el otro lado: el bin víctima quedó con menos de lo que su último
        movimiento declaró. Un cuadre por sumas de toda la red tampoco lo vería
        — el total no cambia, se mueve de una bodega a otra.
        """
        from app.models.almacen import Almacen
        from app.models.inventario import MovimientoInventario

        otro = Almacen(codigo='ALM-NC2', nombre='Neiva Centro',
                       bodega_siesa_id='NC1', activo=True)
        db.session.add(otro)
        db.session.flush()

        # El bin de la tienda pierde 9 unidades y el movimiento se escribe
        # contra el almacén origen, sin nombrar el bin — tal cual lo hace el
        # fallback de despacho.
        kardex.registro.cantidad -= 9
        db.session.add(MovimientoInventario(
            producto_id=kardex.producto.id, ubicacion_id=None,
            almacen_id=otro.id, tipo='SALIDA_TRASLADO', cantidad=-9,
            saldo_antes=9, saldo_despues=0,
            motivo='Traslado TR-0001 → PUNTO DE VENTA'))
        db.session.commit()

        r = _res('INV-01')
        assert r['total'] == 1, 'INV-01 no vio el bin vaciado desde otra bodega'
        assert r['hallazgos'][0]['datos']['diferencia'] == -9
        # Y el movimiento sin ubicación queda declarado, para que el hallazgo se
        # pueda leer junto con su causa probable.
        assert _res('INV-09')['hallazgos'][0]['datos']['movimientos_sin_ubicacion'] == 1


class TestLaVentanaSeDeclara:
    """«0 hallazgos» sobre una ventana no declarada es una afirmación sobre lo
    que no se leyó. Un conteo sin su base no es una medición."""

    def test_declara_las_claves_ciegas(self, db, kardex):
        from app.models.inventario import MovimientoInventario
        db.session.add(MovimientoInventario(
            producto_id=kardex.producto.id, ubicacion_id=kardex.ubicacion.id,
            almacen_id=kardex.almacen_id, tipo='SHORT_PICK', cantidad=1,
            motivo='sin saldos'))
        db.session.commit()

        d = _res('INV-09')['hallazgos'][0]['datos']
        assert d['claves'] == 1
        assert d['claves_ciegas'] == 1
        assert d['claves_comparadas'] == 0
        assert d['ventana_dias'] == 30

    def test_la_ventana_va_en_cada_hallazgo(self, db, kardex):
        """El hallazgo tiene que poder leerse solo, sin volver al reporte."""
        kardex.registro.cantidad -= 4
        db.session.commit()
        assert _res('INV-01')['hallazgos'][0]['datos']['ventana_dias'] == 30

    def test_un_movimiento_sin_fecha_no_desaparece(self, db, kardex):
        """No cae en ninguna ventana. Dejarlo invisible sería un hueco que
        nadie puede contar (Regla 0).

        Se escribe con SQL crudo **porque el ORM no deja**: `fecha` tiene
        `default=utcnow` y rellena el `None`. Pero la columna es nullable, así
        que la fila es representable —una carga masiva, una migración, un
        `INSERT` a mano— y el filtro de ventana la excluiría en silencio.
        """
        from sqlalchemy import text
        db.session.execute(text(
            'UPDATE movimientos_inventario SET fecha = NULL WHERE id = ('
            'SELECT MIN(id) FROM movimientos_inventario)'))
        db.session.commit()
        assert _res('INV-09')['hallazgos'][0]['datos']['movimientos_sin_fecha'] == 1

    def test_sobre_una_base_sin_movimientos_no_declara_nada(self, db):
        """Una instalación recién desplegada no necesita una línea que
        interpretar."""
        r = auditoria.auditar('inventario')
        assert r['hallazgos_totales'] == 0
        assert r['errores'] == []


class TestLosInvariantesNoSonDeTexto:
    """Los detectores de texto se atraparon en sus propios docstrings siete
    veces en una semana. Este archivo declara por AST lo que el módulo mide."""

    def test_el_modulo_no_lee_su_propio_fuente(self):
        """Un invariante que abriera un `.py` estaría midiendo el código y no
        los datos — y se atraparía a sí mismo."""
        import ast
        import pathlib

        arbol = ast.parse(pathlib.Path('app/services/auditoria/inventario.py')
                          .read_text())
        sospechosas = {'read_text', 'open', 'findall', 'search', 'compile'}
        usadas = {getattr(n.func, 'attr', None) or getattr(n.func, 'id', None)
                  for n in ast.walk(arbol) if isinstance(n, ast.Call)}
        assert not (usadas & sospechosas), usadas & sospechosas

    def test_toda_consulta_del_modulo_ordena_antes_de_topar(self):
        """El tope llenado con lo más viejo es como una auditoría deja de ver
        las violaciones nuevas mientras informa «0 hallazgos»."""
        import ast
        import pathlib

        arbol = ast.parse(pathlib.Path('app/services/auditoria/inventario.py')
                          .read_text())
        limites = [n for n in ast.walk(arbol) if isinstance(n, ast.Call)
                   and getattr(n.func, 'attr', None) == 'limit']
        assert limites
        assert all('order_by' in ast.dump(n) for n in limites)
