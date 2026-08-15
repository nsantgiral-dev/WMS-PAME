"""
Invariantes del conteo cíclico: sesión → conteo → descuadre → segundo conteo →
ajuste en Siesa (142951, clase 63).

## Por qué este flujo se audita distinto

Un ajuste de inventario **no lo reclama nadie**. Una venta mal facturada la
llama el cliente; un traslado atascado lo pregunta la tienda. Un ajuste de más
o de menos entra al ERP, cuadra el papel con la realidad equivocada, y la
diferencia solo aparece en el siguiente conteo físico — meses después, cuando
ya no se puede saber de dónde salió.

Por eso lo que se vigila acá no es que las cifras se muevan, sino que **cada
ajuste tenga detrás una cuenta física** y que la dirección del movimiento
coincida con el signo de la diferencia.

## El motivo importa tanto como el número

`AJ-ENT` (sobrante) y `AJ-SAL` (faltante) son motivos distintos en Siesa, con
naturaleza contable opuesta. Un ajuste con el motivo cruzado mueve el
inventario en la dirección correcta y la contabilidad en la contraria.
"""
from app.services.auditoria.base import _AUDITORIA_TRUNCADA,  AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


def _sesiones(estados=None, limite=2000):
    """Las filas a auditar, **las más recientes primero**.

    `order_by(id.desc())` no es cosmética. El `.limit()` corta ANTES de que los
    invariantes filtren —el predicado corre en Python sobre lo que ya volvió—,
    así que sin orden el tope se llena con las filas más viejas y **en cuanto un
    flujo pasa el límite la auditoría deja de ver las violaciones nuevas**. El
    panel diría «0 hallazgos» porque dejó de mirar, no porque esté limpio.

    Y cuando el tope se alcanza se **declara**: `truncado` cuenta HALLAZGOS y no
    tiene nada que ver con esto. Son dos truncamientos distintos y uno estaba
    invisible.
    """
    from app.models.conteo import SesionConteo
    q = SesionConteo.query
    if estados:
        q = q.filter(SesionConteo.estado.in_(estados))
    filas = q.order_by(SesionConteo.id.desc()).limit(limite).all()
    if len(filas) >= limite:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:{limite}')
    return filas


@invariante(
    codigo='CNT-01',
    flujo='conteo',
    frontera='conteo → diferencia',
    consecuencia='La diferencia registrada no es la que sale de las cifras: el '
                 'ajuste que se mande a Siesa va a mover una cantidad que '
                 'nadie contó.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestElDetectorNoEstaCiego::test_ve_una_diferencia_que_no_es_la_resta',
)
def la_diferencia_es_la_resta(ctx=None):
    """`diferencia = cantidad_fisica − existencia_siesa`.

    Se guarda calculada en vez de derivarse al leer, así que puede quedar
    desincronizada si alguien edita una de las dos puntas. Y es el número que
    viaja al ajuste.
    """
    out = []
    for s in _sesiones():
        if s.cantidad_fisica is None or s.existencia_siesa is None:
            continue
        esperada = s.cantidad_fisica - s.existencia_siesa
        if s.diferencia is not None and s.diferencia != esperada:
            out.append(Hallazgo(
                referencia=s.codigo or f'conteo#{s.id}',
                detalle=f'diferencia={s.diferencia} pero '
                        f'{s.cantidad_fisica} − {s.existencia_siesa} = {esperada}',
                datos={'estado': s.estado},
            ))
    return out


@invariante(
    codigo='CNT-02',
    flujo='conteo',
    frontera='diferencia → motivo',
    consecuencia='El motivo contradice el signo: el inventario se mueve en una '
                 'dirección y la contabilidad en la otra.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestElDetectorNoEstaCiego::test_ve_el_motivo_cruzado',
)
def el_motivo_concuerda_con_el_signo(ctx=None):
    """`AJ-ENT` es sobrante (diferencia > 0), `AJ-SAL` es faltante (< 0).

    Son motivos distintos en Siesa con naturaleza contable opuesta —regla del
    concepto 603—, así que cruzarlos no es cosmético.
    """
    out = []
    for s in _sesiones():
        mot = (s.motivo_codigo or '').upper()
        if not mot or s.diferencia is None or s.diferencia == 0:
            continue
        esperado = 'AJ-ENT' if s.diferencia > 0 else 'AJ-SAL'
        if mot != esperado:
            out.append(Hallazgo(
                referencia=s.codigo or f'conteo#{s.id}',
                detalle=f'diferencia={s.diferencia} con motivo {mot} '
                        f'(correspondía {esperado})',
                datos={'estado': s.estado},
            ))
    return out


@invariante(
    codigo='CNT-03',
    flujo='conteo',
    frontera='conteo → Siesa',
    consecuencia='Se mandó un ajuste de inventario sin que nadie contara nada. '
                 'El ERP queda cuadrado contra una cifra inventada.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestElDetectorNoEstaCiego::test_ve_un_ajuste_sin_cuenta_fisica',
)
def ningun_ajuste_sin_cuenta_fisica(ctx=None):
    """`siesa_triggered` con `cantidad_fisica` en `NULL`.

    `NULL` no es cero: cero es «la ubicación está vacía y lo verifiqué», `NULL`
    es «nadie fue a mirar». Ajustar sobre lo segundo es escribir en el ERP un
    número que no salió de ningún lado.
    """
    return [
        Hallazgo(
            referencia=s.codigo or f'conteo#{s.id}',
            detalle='ajuste disparado a Siesa sin cantidad física registrada',
            datos={'estado': s.estado, 'producto': s.producto_codigo_siesa},
        )
        for s in _sesiones()
        if s.siesa_triggered and s.cantidad_fisica is None
    ]


@invariante(
    codigo='CNT-04',
    flujo='conteo',
    frontera='descuadre → segundo conteo',
    consecuencia='Se ajustó un descuadre sin la segunda cuenta que el proceso '
                 'exige. El ajuste descansa en una sola persona contando una '
                 'sola vez.',
    # BLOQUEA, no AVISA: un ajuste de inventario aprobado sobre una sola cuenta
    # de una sola persona no es un aviso. Era AVISA mientras el guard no podía
    # dispararse —preguntaba por la existencia de la fila, y el camino de salto
    # la conserva—, así que la severidad nunca se puso a prueba.
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestElDetectorNoEstaCiego::test_ve_un_ajuste_sin_segundo_conteo',
)
def un_descuadre_se_cuenta_dos_veces(ctx=None):
    """El segundo conteo existe porque la causa más común de un descuadre es un
    error de conteo, no de inventario. Saltárselo convierte un error humano en
    un ajuste permanente."""
    out = []
    for s in _sesiones(('AJUSTADO',)):
        if s.diferencia in (None, 0):
            continue
        if s.es_segundo_conteo or s.sesion_origen_id:
            continue
        # **¿Alguien contó de verdad?** — no «¿existe una fila hija?».
        #
        # `POST /api/conteo/<id>/omitir-segundo` es el ÚNICO camino que salta la
        # doble ciega, y deja el hijo en `CANCELADO` **con su
        # `sesion_origen_id` intacto** (`routes/conteo.py:784`). Preguntando por
        # la existencia de la fila, el endpoint diseñado para saltarse el
        # control producía exactamente el dato que hacía decir «sí, se contó dos
        # veces».
        #
        # El guard medía presencia de fila cuando la propiedad es *hubo una
        # segunda cuenta*, y estaba en verde sobre el único caso que le importa.
        from app.models.conteo import SesionConteo
        hay = SesionConteo.query.filter(
            SesionConteo.sesion_origen_id == s.id,
            SesionConteo.cantidad_fisica.isnot(None),
            SesionConteo.estado.notin_(('CANCELADO', 'PENDIENTE')),
        ).first()
        if not hay:
            out.append(Hallazgo(
                referencia=s.codigo or f'conteo#{s.id}',
                detalle=f'ajustado con diferencia {s.diferencia} sin segundo conteo',
                datos={'producto': s.producto_codigo_siesa},
            ))
    return out


@invariante(
    codigo='CNT-05',
    flujo='conteo',
    frontera='ajuste → Siesa',
    consecuencia='Sesiones que quedaron a mitad del ajuste: el lock se liberó '
                 'y el movimiento a Siesa nunca se confirmó.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestElDetectorNoEstaCiego::test_ve_una_sesion_atascada_ajustando',
)
def ninguna_sesion_se_queda_ajustando(ctx=None):
    """`AJUSTANDO` es una transición —lock liberado, Siesa en vuelo—, no un
    estado de reposo. Una sesión que se quedó ahí no está ni contada ni
    ajustada, y ningún proceso la va a retomar."""
    return [
        Hallazgo(
            referencia=s.codigo or f'conteo#{s.id}',
            detalle=f'atascada en AJUSTANDO · diferencia={s.diferencia} · '
                    f'siesa_triggered={bool(s.siesa_triggered)}',
            datos={'producto': s.producto_codigo_siesa},
        )
        for s in _sesiones(('AJUSTANDO',))
    ]


@invariante(
    codigo='CNT-06',
    flujo='conteo',
    frontera='conteo → ajuste',
    consecuencia='Descuadres detectados que nadie resolvió. Cada uno es una '
                 'diferencia conocida que el inventario sigue arrastrando.',
    severidad=OBSERVA,
    detector_ciego='tests/flujo/test_flujo_conteo.py::TestElDetectorNoEstaCiego::test_cuenta_los_descuadres_abiertos',
)
def se_pueden_contar_los_descuadres_abiertos(ctx=None):
    return [
        Hallazgo(
            referencia=s.codigo or f'conteo#{s.id}',
            detalle=f'{s.estado} · diferencia {s.diferencia}',
            datos={'producto': s.producto_codigo_siesa},
        )
        for s in _sesiones(('DESCUADRE', 'SEGUNDO_CONTEO', 'TERCER_CONTEO'))
    ]


@invariante(
    codigo='CNT-07',
    flujo='conteo',
    frontera='existencia → ajuste',
    consecuencia='El ajuste salió a Siesa como delta sobre una base tomada del '
                 'WMS. Siesa queda en `siesa_real + (fisica − wms)` en vez de '
                 'en `fisica`: **el ajuste empeoró el descuadre justo cuando '
                 'las dos bases discrepaban**, que es la única razón para contar.',
    severidad=BLOQUEA,
    detector_ciego='tests/test_fuente_existencia.py::TestCNT07::test_un_ajuste_sobre_base_del_wms_bloquea',
)
def ningun_ajuste_sobre_una_base_del_wms(ctx=None):
    """El defecto que era inauditable hasta que existió la columna.

    `existencia_siesa` prometía procedencia y no la tenía: ante Siesa caído el
    servicio caía al stock del WMS **con solo un WARNING** y lo guardaba en el
    mismo campo. Sin registro de la base, ningún invariante podía comprobarlo.

    Desde el 2026-08-15 la aprobación **se niega** en ese caso —decisión de
    Operaciones: el ajuste de inventario es la única transacción que siempre
    puede esperar—. Este invariante cubre lo que ya salió antes de esa fecha, y
    el día que alguien reintroduzca el fallback.

    `NULL` no cuenta: es el histórico anterior a la columna, y darlo por bueno
    o por malo sería inventar la procedencia en el campo que existe para no
    inventarla. Solo bloquea el `'WMS'` **explícito**.
    """
    return [
        Hallazgo(
            referencia=s.codigo or f'conteo#{s.id}',
            detalle=f'ajuste de {s.diferencia} enviado a Siesa con la existencia '
                    f'tomada del WMS ({s.existencia_siesa}) — la base no era la '
                    f'fiscal',
            datos={'producto': s.producto_codigo_siesa, 'estado': s.estado},
        )
        for s in _sesiones()
        if s.siesa_triggered and s.fuente_existencia == 'WMS'
    ]
