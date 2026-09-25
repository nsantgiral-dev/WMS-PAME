"""Trinquete: **ninguna unidad se resta sin que se declare a dónde fue.**

## El error que este archivo hace imposible repetir

`PickingService.auditar_tarea(resultado='AVERIA')` restaba del origen aunque no
existiera ningún destino, y escribía un movimiento que decía «trasladada a zona
AVERIAS». Sin destino eso no es un traslado: es una **pérdida de inventario con
un registro que miente**, y salía con HTTP 200.

Existía en la base, en `origin/main` y en la rama. Ningún test lo veía porque la
rama que mueve inventario nunca había corrido en una prueba, y se disparaba
justo en el almacén que todavía no armó su zona de averías — o sea, en el punto
de partida.

## Por qué un inventario declarado y no una regla automática

Detectar «esto resta y aquello suma» por análisis estático es adivinar: el
crédito puede estar tres funciones más allá, en otra transacción, o ser
legítimamente inexistente (una salida a cliente no se acredita en ninguna
ubicación — sale del almacén). Una heurística así produce falsos positivos, y un
guard con falsos positivos **se termina desactivando**, que es peor que no
tenerlo.

Así que la propiedad que se afirma es más modesta y se sostiene sola: **cada
sitio que resta está declarado, con su contrapartida escrita por una persona.**
Agregar el número 11 pone rojo este test hasta que alguien diga a dónde van esas
unidades. Es el trinquete de los endpoints huérfanos aplicado al inventario: no
exige cero, exige que la lista no crezca sin decisión.
"""
import ast
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Cada sitio de `app/` que resta unidades de una `UbicacionProducto`, con la
#: contrapartida que lo hace legítimo. La clave es `archivo::linea_aprox` y el
#: valor es **a dónde van las unidades**.
#:
#: La línea es aproximada a propósito: se compara el conjunto de ARCHIVOS y el
#: CONTEO por archivo, no el número de línea, para que un comentario nuevo no
#: ponga rojo el trinquete. Lo que no puede cambiar sin decisión es cuántos
#: sitios restan y en qué archivo están.
RESTAS_DECLARADAS = {
    'app/routes/inventario.py': (
        1,
        'Ajuste manual de inventario con motivo escrito. La contrapartida es '
        'el `MovimientoInventario` de tipo SALIDA/AJUSTE que el mismo endpoint '
        'escribe: la unidad sale del WMS a propósito y queda el rastro de quién '
        'y por qué. NOTA: no encola nada a Siesa — desacuadra contra el ERP y '
        'eso está sin resolver (2026-09-14).'
    ),
    'app/services/devolucion_cliente_service.py': (
        1,
        '2026-09-24 · m045devol · `liberar_reingreso`: con la NC aprobada en '
        'Siesa, lo devuelto SANO sale del bin DEVOLUCIONES (zona no vendible) '
        'hacia el slot de picking del producto o la ubicación óptima. La '
        'contrapartida es el `UbicacionProducto` del destino, en la misma '
        'función y transacción, con dos `MovimientoInventario` '
        'LIBERACION_DEVOLUCION (salida y entrada). Es un traslado entre zonas '
        'de la misma bodega: el total del almacén no cambia.'
    ),
    'app/services/layout_service.py': (
        1,
        'Traspaso de SIESA-GENERAL hacia un hueco real durante la asignación. '
        'La contrapartida es el `UbicacionProducto` del hueco destino, en la '
        'misma transacción.'
    ),
    'app/services/picking_service.py': (
        4,
        'Cuatro caminos: (1) confirmar picking — sale del almacén hacia el '
        'cliente, sin contrapartida en ubicación por definición; (2) auditoría '
        'NO_ENCONTRADO/PARCIAL — faltante confirmado, la contrapartida es el '
        'movimiento que lo declara; (3) auditoría AVERIA — la contrapartida es '
        'el bin de averías, GARANTIZADO por `_destino_averias_garantizado`, que '
        'nunca devuelve None; (4) short-pick. 2026-09-14: el (3) es el que se '
        'arregló — restaba aunque no hubiera destino.'
    ),
    'app/services/reposicion_service.py': (
        1,
        'Traslado interno RESERVA → PICKING. La contrapartida es el '
        '`UbicacionProducto` de la ubicación de picking, en la misma función.'
    ),
    'app/services/siesa_job_service.py': (
        2,
        'Dos ajustes AJ-SAL que vienen de Siesa (conteo cíclico y ajuste '
        'físico). La contrapartida vive en Siesa, no en el WMS: el WMS está '
        'reflejando un movimiento que ya ocurrió allá.'
    ),
    'app/services/traslado_service.py': (
        2,
        'Dos restas, y las dos tienen contrapartida.\n'
        '· Salida por traslado entre sedes: la contrapartida es la bodega '
        'destino en Siesa; en el WMS queda el `MovimientoInventario` '
        'SALIDA_TRASLADO.\n'
        '· 2026-09-18 · `_descontar_del_bucket_vendible`: al confirmar una '
        'avería, las unidades salen del stock vendible y entran al bin de '
        'averías **en la misma transacción**, con su `MovimientoInventario` '
        'ENTRADA. No es una resta sin destino: es un traslado entre zonas de '
        'la misma bodega, y por eso el total del almacén no cambia. Sin ella, '
        'las mismas unidades quedaban contadas dos veces —en el bucket y en '
        'el bin— hasta la carga de las 7am del día siguiente.'
    ),
}


def _sitios_que_restan(base: pathlib.Path = None):
    """Todo sitio de `app/` que resta de un atributo `.cantidad`.

    Por AST y no por texto: `reg.cantidad -= n` y
    `reg.cantidad = max(0, reg.cantidad - n)` son la misma operación escrita de
    dos formas, y una regex que busque `-=` ve solo la mitad. Es la lección de
    los detectores de texto de este repo, aplicada al inventario.
    """
    base = base or RAIZ
    encontrados = {}
    for f in sorted((base / 'app').rglob('*.py')):
        rel = str(f.relative_to(base))
        try:
            arbol = ast.parse(f.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for n in ast.walk(arbol):
            if (isinstance(n, ast.AugAssign)
                    and isinstance(n.target, ast.Attribute)
                    and n.target.attr == 'cantidad'
                    and isinstance(n.op, ast.Sub)):
                encontrados.setdefault(rel, []).append(n.lineno)
            elif (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Attribute)
                    and n.targets[0].attr == 'cantidad'
                    and any(isinstance(x, ast.Sub) for x in ast.walk(n.value))):
                encontrados.setdefault(rel, []).append(n.lineno)
    return encontrados


class TestNingunaRestaDeInventarioSinDeclarar:

    def test_la_lista_no_crece_sin_decision(self):
        """Un sitio nuevo que resta inventario pone esto rojo hasta que alguien
        escriba a dónde van las unidades."""
        hallados = _sitios_que_restan()
        nuevos = {a: ls for a, ls in hallados.items()
                  if a not in RESTAS_DECLARADAS}
        assert not nuevos, (
            '\nArchivos que restan inventario y NO están declarados:\n'
            + '\n'.join(f'  · {a} (líneas {ls})' for a, ls in nuevos.items())
            + '\n\nCada resta necesita su contrapartida escrita: a dónde van '
              'esas unidades.\nRestar sin acreditar no es un traslado — es una '
              'pérdida, y el movimiento\nque la acompaña miente sobre lo que '
              'pasó. Agregá el archivo a RESTAS_DECLARADAS\ncon el conteo y el '
              'motivo, o acreditá el destino.')

    def test_el_conteo_por_archivo_no_sube(self):
        """Que el archivo esté declarado no autoriza restas nuevas adentro."""
        hallados = _sitios_que_restan()
        subieron = {}
        for archivo, (esperado, _motivo) in RESTAS_DECLARADAS.items():
            real = len(hallados.get(archivo, []))
            if real > esperado:
                subieron[archivo] = (esperado, real, hallados[archivo])
        assert not subieron, (
            '\n' + '\n'.join(
                f'  · {a}: declaradas {e}, encontradas {r} (líneas {ls})'
                for a, (e, r, ls) in subieron.items())
            + '\n\nHay una resta de inventario nueva en un archivo ya '
              'declarado. Actualizá\nel conteo Y el motivo — el motivo es lo '
              'que hace que la próxima persona\nsepa si esa unidad tiene a '
              'dónde ir.')

    def test_la_lista_solo_encoge(self):
        """La otra dirección: si un sitio dejó de restar, sale de la lista. Una
        declaración que sobrevive a su causa se lee como permiso."""
        hallados = _sitios_que_restan()
        sobran = {a: e for a, (e, _m) in RESTAS_DECLARADAS.items()
                  if len(hallados.get(a, [])) < e}
        assert not sobran, (
            f'\nDeclaradas de más: {sobran}\nBajá el conteo — la lista solo '
            f'encoge.')

    def test_toda_declaracion_dice_a_donde_van_las_unidades(self):
        """Un conteo sin motivo es una lista de excepciones, no una política."""
        flojas = {a: m for a, (_e, m) in RESTAS_DECLARADAS.items()
                  if len(m) < 80}
        assert not flojas, (
            f'\nMotivos demasiado cortos para decir a dónde van las unidades: '
            f'{list(flojas)}')


class TestElDetectorVeLasDosFormasDeRestar:
    """Meta-tests. Un escáner que se desincroniza devuelve cero y se lee igual
    que «acá no hay nada que hacer» — ya pasó en esta misma sesión con un
    barrido que reportó cero en tres archivos por un literal de regex.

    Corren el escáner REAL sobre un árbol de mentira, con un parámetro de base.
    Parchear `rglob` probaría el parche, no el escáner.
    """

    def _sitios_en(self, fuente, tmp_path):
        f = tmp_path / 'app' / 'x.py'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fuente, encoding='utf-8')
        return _sitios_que_restan(base=tmp_path)

    def test_ve_la_forma_augassign(self, tmp_path):
        assert self._sitios_en('reg.cantidad -= 5\n', tmp_path)

    def test_ve_la_forma_max_cero(self, tmp_path):
        """La que una regex de `-=` no ve, y es la que tenía el bug."""
        assert self._sitios_en('reg.cantidad = max(0, reg.cantidad - 5)\n',
                               tmp_path)

    def test_NO_marca_una_suma(self, tmp_path):
        """La otra dirección: un detector que marque todo prueba la mitad."""
        assert not self._sitios_en('reg.cantidad += 5\n', tmp_path)

    def test_NO_marca_otro_atributo(self, tmp_path):
        assert not self._sitios_en('reg.reservado -= 5\n', tmp_path)

    def test_el_barrido_real_encuentra_algo(self):
        """Piso mínimo: si esto da cero, el escáner se rompió — no es que el
        repo dejó de restar inventario."""
        assert sum(len(v) for v in _sitios_que_restan().values()) >= 8


# ─────────────────────────────────────────────────────────────────────────────
# La misma unidad restada dos veces — reabrir un picking (P0-7, 2026-09-25)
# ─────────────────────────────────────────────────────────────────────────────
#
# La otra cara de la clase: una resta **declarada** que corre dos veces sobre
# las mismas unidades. `reportar_problema` descuenta lo encontrado (SHORT_PICK);
# `reabrir_picking` ponía `cantidad_recogida` en 0 y el nuevo pick restaba la
# cantidad completa. Con el empaque vivo, las 7 unidades que ya estaban en la
# caja salían del hueco otra vez.
#
# La propiedad se mide sobre el dato, no sobre el código: **después de reabrir
# y volver a recoger, del hueco salió exactamente lo pedido.**

def _escenario(db, almacen, stock=20, pedido=10, encontrada=7, ref='PD7001'):
    from app.models.usuario import Usuario
    from app.services.picking_service import PickingService
    from tests.flujo import conductor_de_flujo as cf
    op = Usuario(nombre='Op reabrir', email=f'op-reab-{ref}@t.co', rol='operario',
                 almacen_id=almacen.id, activo=True)
    op.set_password('x') if hasattr(op, 'set_password') else None
    sup = Usuario(nombre='Sup reabrir', email=f'sup-reab-{ref}@t.co', rol='supervisor',
                  almacen_id=almacen.id, activo=True)
    sup.set_password('x') if hasattr(sup, 'set_password') else None
    db.session.add_all([op, sup])
    db.session.commit()
    productos, ub = cf.sembrar_catalogo(db, almacen, n=1, con_stock=stock)
    p = productos[0]
    t = PickingService.crear_tareas(producto_id=p.id, cantidad=pedido,
                                    almacen_id=almacen.id, referencia_documento=ref,
                                    tipo_documento='PEDIDO')[0]
    PickingService.iniciar_picking(t.id, op.id)
    PickingService.reportar_problema(t.id, op.id, 'FALTANTE', cantidad_encontrada=encontrada)
    db.session.commit()
    return t, p, ub, op, sup


def _en_hueco(db, ub, p):
    from app.models.inventario import UbicacionProducto
    db.session.expire_all()
    return UbicacionProducto.query.filter_by(ubicacion_id=ub.id, producto_id=p.id).first()


class TestReabrirNoRestaDosVeces:

    def test_con_empaque_vivo_solo_se_recoge_el_faltante(self, db, almacen):
        from app.models.picking import TareaPicking
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen)
        assert _en_hueco(db, ub, p).cantidad == 13          # salieron las 7 halladas
        PackingService.crear_desde_picking(
            tareas_picking_ids=[t.id], numero_pedido_siesa='PD7001',
            almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa='7001')

        nueva = PickingService.reabrir_picking(t.id, sup.id, motivo='apareció el resto')
        assert nueva.id != t.id and nueva.cantidad_solicitada == 3
        original = db.session.get(TareaPicking, t.id)
        assert original.estado == 'COMPLETADO' and original.cantidad_recogida == 7

        PickingService.iniciar_picking(nueva.id, op.id)
        PickingService.confirmar_picking(nueva.id, 3, op.id)
        assert _en_hueco(db, ub, p).cantidad == 10, (
            'del hueco salieron más de las 10 pedidas: la reapertura volvió a '
            'restar lo que ya estaba empacado')

    def test_sin_empaque_lo_recogido_vuelve_y_se_recoge_todo(self, db, almacen):
        from app.models.inventario import MovimientoInventario
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7002')
        misma = PickingService.reabrir_picking(t.id, sup.id, motivo='se equivocó de hueco')
        assert misma.id == t.id and misma.cantidad_recogida == 0
        assert _en_hueco(db, ub, p).cantidad == 20            # reingresadas las 7
        assert MovimientoInventario.query.filter_by(
            tipo='REINGRESO_REAPERTURA', cantidad=7).count() == 1, (
            'el reingreso necesita su movimiento: una suma sin rastro es el '
            'espejo de una resta sin destino')

        PickingService.iniciar_picking(t.id, op.id)
        PickingService.confirmar_picking(t.id, 10, op.id)
        assert _en_hueco(db, ub, p).cantidad == 10

    def test_sin_nada_recogido_no_toca_el_stock(self, db, almacen):
        """La otra dirección: un reabrir que siempre reingresa inventaría
        unidades que nunca salieron."""
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, encontrada=0, ref='PD7003')
        PickingService.reabrir_picking(t.id, sup.id, motivo='no estaba en el hueco')
        assert _en_hueco(db, ub, p).cantidad == 20
