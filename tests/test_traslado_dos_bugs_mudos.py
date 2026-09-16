"""Dos fallos que no daban error — los peores de encontrar.

Los dos aparecieron levantando el terreno del flujo satélite→NB1, y los dos
habrían mordido ese flujo. Ninguno rompía nada visible: uno se tragaba su propia
excepción, el otro dejaba un campo en NULL que hacía ciego a un monitor.

## 1 · `EstadoLPN` fuera de alcance

`confirmar_recepcion` usaba `EstadoLPN.CONSUMIDO`, pero `EstadoLPN` estaba
importado **dentro de `despachar()`**. Un `from ... import` adentro de una
función crea un nombre local de ESA función: en `confirmar_recepcion` el nombre
no existe. La línea lanzaba `NameError`, el `except Exception` de al lado lo
convertía en un `warning`, y **los LPN de un traslado recibido se quedaban en
'EN_TRANSITO' para siempre**.

## 2 · `fecha_despacho` solo en uno de los dos caminos

Un traslado se despacha por dos vías: el botón del admin
(`TrasladoService.despachar`) y el cierre de caja del empacador (que encola un
job y la DLQ pone EN_TRANSITO). Solo la primera escribía `fecha_despacho`.

`traslado_monitor_service` filtra `fecha_despacho <= limite`, y un NULL **nunca**
satisface esa comparación — así que el monitor de traslados estancados era
CIEGO a todo lo despachado por el empacador. Y TRA-30 los reportaba como «en
tránsito sin fecha de despacho» sin que nadie supiera de dónde salían.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.models.siesa_job import SiesaJob
from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado, EstadoTraslado
from app.services import siesa_job_service as sjs


def _solicitud(db, usuario, producto, estado=EstadoTraslado.EN_PACKING):
    s = SolicitudTraslado(codigo='ST-BUG-1', bodega_origen_siesa='NB1',
                          bodega_destino_siesa='NC1', estado=estado,
                          modo_transferencia='EN_TRANSITO',
                          solicitante_id=usuario.id)
    db.session.add(s); db.session.flush()
    db.session.add(ItemSolicitudTraslado(
        solicitud_id=s.id, producto_id=producto.id,
        producto_codigo_siesa=producto.codigo_siesa,
        cantidad_solicitada=5, cantidad_aprobada=5, cantidad_enviada=5))
    db.session.commit()
    return s


# ── Bug 2 · la fecha que faltaba en un camino ─────────────────────────────

def test_el_despacho_por_el_cierre_de_packing_deja_fecha(db, usuario, producto):
    """Sin esto el monitor de estancados no los ve nunca."""
    s = _solicitud(db, usuario, producto)
    job = SiesaJob.encolar('DESPACHO_TRASLADO', {'solicitud_id': s.id},
                           referencia_tipo='solicitud_traslado', referencia_id=s.id)
    db.session.commit()

    st = MagicMock()
    st.bodega_transito = 'TRA1'
    st.registrar_salida_transito.return_value = {'consec': 777}
    st.recuperar_consec_salida.return_value = 777
    with patch('app.services.siesa_traslado_adapter.siesa_traslado', st), \
         patch('app.services.connekta_gateway.connekta', MagicMock()):
        sjs._ejecutar_job(job)
    db.session.refresh(s)

    assert s.estado == EstadoTraslado.EN_TRANSITO, s.estado
    assert s.fecha_despacho is not None, (
        'EN_TRANSITO sin fecha_despacho: el monitor de estancados no lo ve')


def test_el_monitor_filtra_por_esa_fecha(db):
    """La razón por la que el NULL importaba: la comparación lo descarta."""
    import inspect
    from app.services import traslado_monitor_service as tms
    fuente = inspect.getsource(tms)
    assert 'fecha_despacho' in fuente, (
        'si el monitor dejó de mirar esa fecha, este test sobra')


# ── Bug 1 · el NameError que se tragaba a sí mismo ────────────────────────

def test_estado_lpn_esta_en_alcance_al_recibir(db):
    """El fallo era de ALCANCE, no de lógica: el nombre existía en otra función.

    Se comprueba sobre el AST: dentro de `confirmar_recepcion` tiene que haber
    un import que traiga `EstadoLPN`. Un test de comportamiento no lo vería —
    el `except Exception` convierte el NameError en un warning y la función
    termina en verde."""
    import ast
    import inspect
    from app.services.traslado_service import TrasladoService

    import textwrap
    fuente = textwrap.dedent(
        inspect.getsource(TrasladoService.confirmar_recepcion))
    arbol = ast.parse(fuente)
    importados = {
        a.name
        for n in ast.walk(arbol) if isinstance(n, ast.ImportFrom)
        for a in n.names
    }
    usados = {
        n.value.id
        for n in ast.walk(arbol)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    assert 'EstadoLPN' not in usados or 'EstadoLPN' in importados, (
        'confirmar_recepcion usa EstadoLPN sin importarlo en su propio alcance: '
        'NameError tragado por el except, y los LPN nunca pasan a CONSUMIDO')
