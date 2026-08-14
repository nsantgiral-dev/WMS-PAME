"""
Qué flujos están vigilados — y, sobre todo, cuáles no.

Un auditor que cubre un flujo de seis se lee igual que uno que los cubre todos:
devuelve `0 hallazgos` para lo que no mira. Este archivo hace visible el
denominador.
"""
import pytest

from app.services import auditoria

#: Flujos con invariantes escritos. Esta lista **solo puede crecer**: si un
#: flujo desaparece de acá es que alguien borró sus invariantes, y eso tiene
#: que doler.
FLUJOS_CUBIERTOS = {'venta', 'traslados', 'conteo', 'devoluciones', 'recepcion'}

#: Flujos del WMS que mueven inventario o dinero y **todavía no tienen
#: invariantes**. Escrito para que se vea, no para que se olvide.
SIN_CUBRIR = {
    'reposicion': 'RESERVA→PICKING. Mueve inventario entre ubicaciones (173066).',
    'flota': 'Custodia de vehículos. No mueve inventario ni dinero.',
    'tienda': 'Pedidos de tienda. Se apoya en traslados, que sí está cubierto.',
    'compras_ia': 'Propuestas, no ejecuta movimientos.',
    'kardex': 'Lectura y reconstrucción. Su compuerta ya tiene su propio guard.',
    'vigia': 'Detección estadística; su certificación es otro arnés.',
}


class TestElDenominadorEsVisible:

    def test_los_flujos_cubiertos_siguen_estando(self):
        faltan = FLUJOS_CUBIERTOS - set(auditoria.flujos())
        assert not faltan, f'se perdieron invariantes de: {faltan}'

    def test_cada_flujo_tiene_al_menos_un_bloqueante(self):
        """Un flujo con solo `OBSERVA` no vigila: informa."""
        for f in FLUJOS_CUBIERTOS:
            inv = auditoria.registrados(f)
            assert any(i.severidad == auditoria.BLOQUEA for i in inv), f

    def test_todo_invariante_declara_su_consecuencia(self):
        """En términos de operación, no de código: quien lee el reporte no
        necesariamente sabe qué es un `producto_id`."""
        for i in auditoria.registrados():
            assert i.consecuencia and len(i.consecuencia) > 40, i.codigo
            assert i.frontera, i.codigo

    def test_los_codigos_no_se_repiten(self):
        codigos = [i.codigo for i in auditoria.registrados()]
        assert len(codigos) == len(set(codigos))

    def test_lo_que_falta_esta_escrito(self):
        """El punto del archivo. Si alguien agrega un flujo nuevo sin
        invariantes, tiene que decidirlo a propósito: o lo cubre, o lo declara
        acá con el motivo."""
        assert SIN_CUBRIR
        assert all(len(v) > 20 for v in SIN_CUBRIR.values())

    def test_ningun_flujo_declarado_sin_cubrir_tiene_invariantes(self):
        """Coherencia entre las dos listas: si alguien cubre un flujo y olvida
        sacarlo de `SIN_CUBRIR`, la lista miente sobre lo que falta."""
        solapan = SIN_CUBRIR.keys() & set(auditoria.flujos())
        assert not solapan, f'ya están cubiertos y siguen listados como pendientes: {solapan}'


class TestTodosCorrenSobreUnaBaseVacia:
    """Sin datos no debe romperse nada ni inventarse hallazgos.

    Es el estado de una instalación recién desplegada, y es cuando alguien va a
    abrir el panel por primera vez.
    """

    @pytest.mark.parametrize('flujo', sorted(FLUJOS_CUBIERTOS))
    def test_sin_datos_no_hay_hallazgos_ni_errores(self, app, db, flujo):
        r = auditoria.auditar(flujo)
        assert r['errores'] == []
        assert r['hallazgos_totales'] == 0
        assert r['invariantes_corridos'] > 0
