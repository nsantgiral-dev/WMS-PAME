"""
Validación crítica de compras, tanda A+C (2026-09-27) — lo que el primer
arreglo NO cerraba, reproducido. Eran cuatro `xfail(strict=True)`; cerrados el
mismo día (CLAUDE.md «Existencias verdaderas» y «La venta de Siesa») y sin
marca desde entonces.

1. P0 · La lectura completa nunca se escribe en producción: la «defensa en
   profundidad» `_verificar_respuesta_no_parcial` compara las filas de NB1 que
   Siesa trae contra `ubicaciones_productos` con cantidad > 0 de TODOS los
   almacenes (sin `almacen_id`), y ese baseline además está inflado por los
   mismos fantasmas que el arreglo quiere limpiar. Producción 27-sep: Siesa NB1
   4.588 filas; `ubicaciones_productos` > 0: 31.740 global, 7.975 en NB1.
   4.588 < 0,7 × 31.740 → «Respuesta parcial» → degradado → no se escribe
   `stock_siesa`, la reconciliación se niega y la carga física no escribe.
2. P1 · Una lectura incompleta (de día el total cambia) invalida la lectura
   completa del mismo día que ya estaba en memoria: la carga de las 7:00 fuerza
   una lectura nueva, si sale incompleta la carga no escribe aunque la de las
   6:xx fuera completa y de hoy.
3. P1 · Un período del histórico registrado con `hasta` antes de su fin queda
   `completa`; si la reciente (14 días) no alcanzó esos días, el hueco es
   permanente y nadie lo lista: contenedor (180 d) y temporada (360 d) nunca
   llegan.
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.services import inventario_siesa_service as iss
from tests.test_existencias_verdaderas import (SiesaExistencias, cache_limpio,  # noqa: F401
                                               fila_bd, refrescar, sembrar, universo)
from tests.test_demanda_historico import SiesaVentas, _filas


def _ubicaciones(db, almacen_id, n, prefijo):
    from app.models.inventario import UbicacionProducto
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    gen = Ubicacion(codigo=f'{Ubicacion.CODIGO_GENERAL}', almacen_id=almacen_id,
                    zona='GENERAL', activo=True)
    db.session.add(gen)
    db.session.flush()
    for i in range(n):
        p = Producto(codigo=f'{prefijo}{i:04d}', nombre='x', codigo_siesa=f'{prefijo}{i:04d}',
                     activo=True)
        db.session.add(p)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=gen.id, producto_id=p.id, cantidad=3))
    db.session.commit()


class TestLaLecturaCompletaSeEscribeConElInventarioDeProduccion:

    def test_tiendas_cargadas_no_bloquean_la_lectura_de_nb1(self, db, almacen, cache_limpio):
        """Estado limpio: NB1 tiene en el WMS lo mismo que Siesa (80), pero NS1
        tiene 200 ubicaciones con stock (como en producción, donde NS1 y NC1
        tienen la carga física). La lectura completa debe escribirse."""
        from app.models.almacen import Almacen
        ns1 = Almacen(codigo='ALM-NS1', nombre='Neiva Sur', bodega_siesa_id='NS1', activo=True)
        db.session.add(ns1)
        db.session.commit()
        _ubicaciones(db, almacen.id, 80, 'NB')
        _ubicaciones(db, ns1.id, 200, 'NS')
        sembrar(db, 'NB1', 'PAPELSP1003', 1508)
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))
        assert not iss._cache_inventario_multibodega['degradado'], \
            iss._cache_inventario_multibodega['motivo']
        assert fila_bd('NB1', 'PAPELSP1003').existencia == 0

    def test_los_fantasmas_del_wms_no_protegen_a_los_fantasmas(self, db, almacen, cache_limpio):
        """Producción: Siesa NB1 4.588 filas; el WMS tiene 7.975 ubicaciones con
        stock en NB1 (la carga física escribió la mezcla con fantasmas). 57 % <
        70 % → «respuesta parcial» para siempre. Aquí: 80 contra 150."""
        _ubicaciones(db, almacen.id, 150, 'NB')
        sembrar(db, 'NB1', 'PAPELSP1003', 1508)
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))
        assert fila_bd('NB1', 'PAPELSP1003').existencia == 0


class TestUnaLecturaIncompletaNoBorraLaCompletaDeHoy:

    def test_la_completa_de_la_madrugada_sigue_sirviendo(self, db, cache_limpio):
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))           # 6:xx, completa
            assert iss.fuente_para_escribir('NB1') == ''
            refrescar(SiesaExistencias(universo(250), total_en={2: 251}))   # 7:00, se movió
            assert iss.fuente_para_escribir('NB1') == '', iss.fuente_para_escribir('NB1')


class TestElHuecoDelPeriodoEnCursoNoQuedaCallado:

    def test_el_trimestre_registrado_a_medias_vuelve_a_la_lista(self, app, db):
        from app.models.demanda_siesa import DemandaDiaCubierto
        from app.services import demanda_fuentes as dfu
        hoy = date(2026, 10, 20)
        t4, t3 = dfu.ventanas_historicas(hoy)[0], dfu.ventanas_historicas(hoy)[1]
        assert t3['consulta'].endswith('2026T3')
        registrado_hasta = date(2026, 9, 20)       # el día que se copió el SQL
        gw = SiesaVentas({t3['consulta']: (_filas(t3['desde'], registrado_hasta, refs=('A',),
                                                  bodegas=('NB1',)),
                                           t3['desde'], registrado_hasta)})
        r = dfu.descargar_ventana(t3, gateway=gw, pausa_s=0, reloj=gw.reloj)
        assert r['completa']
        # La reciente empezó a correr tarde: cubre del 6 al 19 de octubre; T4
        # (1 al 19) se registró y leyó. Del 21 al 30 de sept. no lo cubre nadie.
        d = date(2026, 10, 1)
        while d <= hoy - timedelta(days=1):
            db.session.add(DemandaDiaCubierto(fecha=d))
            d += timedelta(days=1)
        db.session.commit()
        faltan = [x for x in range(21, 31)
                  if date(2026, 9, x) not in dfu.cobertura_siesa(con_dias=True)['dias_cubiertos']]
        assert faltan, 'el hueco existe'
        pendientes = [p['consulta'] for p in dfu.ventanas_pendientes(hoy)]
        assert t3['consulta'] in pendientes, (
            'el hueco del 21 al 30 de septiembre corta la cobertura para siempre y '
            '«qué hacer» no lo dice')
