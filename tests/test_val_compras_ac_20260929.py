"""
Re-validación de compras A+C (2026-09-29): lo que los arreglos de 51e6b277..c55c6f59
todavía no cerraban, reproducido. Era un `xfail(strict=True)`; cerrado el mismo
día (`_asegurar_ultima_completa` recarga de la base cuando el último OK es más
nuevo que el de memoria) y sin marca desde entonces.

P1 · Un proceso que no hizo la lectura (la web: en producción solo el worker
corre `[INV_SIESA_REFRESH]`, medido en `registros_sync`) se queda para siempre
con la lectura completa que cargó de la base al arrancar. `_asegurar_ultima_completa`
devuelve `True` si hay una completa en memoria, aunque sea de ayer y la base ya
tenga la de hoy. El botón «Cargar inventario» y la reconciliación corren en la
web: al día siguiente responden «la última lectura completa no es de hoy»
aunque el worker la hizo a las 04:30.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.services import inventario_siesa_service as iss
from tests.test_existencias_verdaderas import (SiesaExistencias, cache_limpio,  # noqa: F401
                                               refrescar, universo)


class TestOtroProcesoVeLaLecturaDeHoy:

    def test_la_web_ve_la_lectura_de_las_0430_del_worker(self, db, cache_limpio):
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))          # el worker, hoy 04:30
            # La web cargó ayer la completa de ayer y la conserva en memoria.
            c = iss._cache_inventario_multibodega
            c['ts'] = c['ts'] - timedelta(days=1)
            assert iss.fuente_para_escribir('NB1') == '', iss.fuente_para_escribir('NB1')
