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
from app.services.auditoria.base import AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


def _sesiones(estados=None, limite=2000):
    from app.models.conteo import SesionConteo
    q = SesionConteo.query
    if estados:
        q = q.filter(SesionConteo.estado.in_(estados))
    return q.limit(limite).all()


@invariante(
    codigo='CNT-01',
    flujo='conteo',
    frontera='conteo → diferencia',
    consecuencia='La diferencia registrada no es la que sale de las cifras: el '
                 'ajuste que se mande a Siesa va a mover una cantidad que '
                 'nadie contó.',
    severidad=BLOQUEA,
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
    severidad=AVISA,
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
        # ¿Existe una sesión de segundo conteo que apunte a ésta?
        from app.models.conteo import SesionConteo
        hay = SesionConteo.query.filter_by(sesion_origen_id=s.id).first()
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
