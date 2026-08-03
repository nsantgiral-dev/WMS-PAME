"""
Traspaso de custodia — `docs/flota/ESPECIFICACION_T1.md` §2.

```
[sin custodia]  → apertura → ACTIVA → cierre → CERRADA
                                 ↘ traspaso ↗
```

**Esto es lo único que previene los huecos de cobertura.** El invariante 3 no lo
puede imponer la base: un hueco lo produce una escritura que NO ocurre, y una
restricción solo juzga escrituras que sí ocurren. Lo que sí se puede garantizar
es que el cierre y la apertura pasen **en la misma transacción y con la misma
marca de tiempo** — así no existe instante intermedio que alguien pueda observar,
ni fila que quede a medio camino si algo falla.

De ahí que el traspaso viva en una función y no en dos llamadas seguidas desde
la pantalla: dos llamadas son dos transacciones, y entre ellas hay un vehículo
sin responsable durante el tiempo que tarde la red del conductor a las 5 a.m.

Regla 3 del módulo: sin odómetro no se persiste ningún evento de flota. Acá se
aplica antes de tocar nada — el kilometraje entra por la misma puerta que la
custodia o no entra ninguno de los dos.
"""
from datetime import datetime
from typing import List, Optional

from app.extensions import db
from app.models.vehiculo import Vehiculo
from flota.adaptadores.modelos import Custodia, Foto, LecturaOdometro
from flota.dominio import custodia as dom
from flota.dominio import odometro as dom_odo
from flota.dominio.errores import CustodiaInvalida
from flota.dominio.valores import (
    Custodia as CustodiaDom,
    CustodioEstado,
    CustodioTipo,
    Lectura,
    OrigenLectura,
    QuienPide,
)

FOTOS_POR_CUSTODIA = 8


def _a_dominio(fila: Custodia) -> CustodiaDom:
    """Traduce la fila a la estructura que el dominio sabe juzgar."""
    return CustodiaDom(
        vehiculo_id=fila.vehiculo_id,
        custodio_tipo=CustodioTipo(fila.custodio_tipo),
        inicio_ts=fila.inicio_ts,
        fin_ts=fila.fin_ts,
        registrado_por_usuario_id=fila.registrado_por_usuario_id,
        km_inicio=fila.km_inicio,
        km_fin=fila.km_fin,
        custodio_conductor_id=fila.custodio_conductor_id,
        custodio_sede_id=fila.custodio_sede_id,
        custodio_estado=CustodioEstado(fila.custodio_estado),
        linea_base=fila.linea_base,
    )


def custodia_activa(vehiculo_id: int) -> Optional[Custodia]:
    """La custodia abierta del vehículo, o `None` si no tiene.

    `None` acá sí significa "no tiene": el índice único parcial garantiza que
    hay cero o una, así que la ausencia es un hecho medido, no un no-sé.
    """
    return Custodia.query.filter(
        Custodia.vehiculo_id == vehiculo_id,
        Custodia.fin_ts.is_(None),
    ).one_or_none()


def traspasar(
    *,
    vehiculo_id: int,
    km: int,
    registrado_por_usuario_id: int,
    custodio_tipo: CustodioTipo,
    custodio_conductor_id: Optional[int] = None,
    custodio_sede_id: Optional[int] = None,
    custodio_estado: CustodioEstado = CustodioEstado.RESUELTO,
    quien_pide: QuienPide = QuienPide.ADMIN_ZONA,
    motivo_forzado: Optional[str] = None,
    fotos_fin: Optional[List[dict]] = None,
    fotos_inicio: Optional[List[dict]] = None,
    ts: Optional[datetime] = None,
) -> Custodia:
    """Cierra la custodia vigente y abre la nueva, atómicamente.

    QUÉ AFIRMA al devolver: que el vehículo tiene exactamente una custodia
    activa, que empieza en el mismo instante en que terminó la anterior, y que
    el kilometraje quedó registrado como lectura con su origen.

    QUÉ NO AFIRMA: que las fotos estén completas. Una custodia con menos de
    ocho se abre igual —el conductor no puede quedarse en el patio— y el health
    la cuenta en `custodias_sin_foto_completa`. Bloquear ahí dejaría camiones
    sin salir por una foto, y la operación desmonta el sistema en 48 horas.

    Levanta antes de tocar nada si el custodio propuesto es inválido. No hay
    estado intermedio observable: o pasa todo, o no pasa nada.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    # ── 1. Juzgar ANTES de escribir ──────────────────────────────────────
    propuesta = CustodiaDom(
        vehiculo_id=vehiculo_id,
        custodio_tipo=custodio_tipo,
        inicio_ts=ahora,
        registrado_por_usuario_id=registrado_por_usuario_id,
        km_inicio=km,
        custodio_conductor_id=custodio_conductor_id,
        custodio_sede_id=custodio_sede_id,
        custodio_estado=custodio_estado,
    )
    dom.validar_arco_exclusivo(propuesta)

    vigente = custodia_activa(vehiculo_id)

    # ¿Puede recibirlo? La decisión es del dominio, no de un `if` acá — una
    # regla escrita en el adaptador se pierde en el próximo refactor.
    mismo_custodio = (
        vigente is not None
        and vigente.custodio_conductor_id is not None
        and vigente.custodio_conductor_id == custodio_conductor_id
    )
    forzado = False
    if vigente is not None and not mismo_custodio:
        nombre = (vigente.custodio_conductor.nombre
                  if vigente.custodio_conductor is not None else '')
        veredicto = dom.puede_recibir(
            vigente, quien_pide,
            nombre_custodio_actual=nombre,
            placa=Vehiculo.query.get(vehiculo_id).placa if vehiculo_id else '',
            desde=vigente.inicio_ts.strftime('%d/%m a las %H:%M'),
        )
        if not veredicto.puede:
            raise CustodiaInvalida(veredicto.mensaje)
        # Forzado no es "cerrar el de otro": es cerrarlo SIN FOTOS DE CIERRE.
        # Ese es el daño concreto — el turno siguiente arranca sin nada con qué
        # comparar, y el próximo golpe que aparezca no se le puede atribuir a
        # nadie. Un admin que cierra con las ocho fotos hace un cierre normal.
        if veredicto.requiere_forzado and not fotos_fin:
            if not (motivo_forzado or '').strip():
                raise CustodiaInvalida(
                    'Cerrar el turno de otro sin su firma exige motivo escrito: '
                    'sin fotos de cierre, es lo único que va a explicar después '
                    'por qué el turno siguiente arrancó sin comparación.'
                )
            forzado = True

    # Regla 3: el kilometraje entra por la misma puerta o no entra ninguno.
    previas = [
        Lectura(valor_km=l.valor_km, ts=l.ts, origen=OrigenLectura(l.origen),
                autor_usuario_id=l.autor_usuario_id,
                motivo_correccion=l.motivo_correccion)
        for l in LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id).all()
    ]
    dom_odo.validar_lectura(previas, Lectura(
        valor_km=km, ts=ahora, origen=OrigenLectura.ENTREGA,
        autor_usuario_id=registrado_por_usuario_id,
    ))

    if vigente is not None and km < vigente.km_inicio:
        raise CustodiaInvalida(
            f'el cierre no puede tener menos kilómetros que la apertura: '
            f'{km} < {vigente.km_inicio}'
        )

    # ── 2. Escribir todo en una transacción ──────────────────────────────
    try:
        if vigente is not None:
            vigente.fin_ts = ahora
            vigente.km_fin = km
            if forzado:
                vigente.cierre_forzado = True
                vigente.cierre_forzado_por_usuario_id = registrado_por_usuario_id
                vigente.cierre_forzado_motivo = motivo_forzado.strip()
            db.session.flush()
            _colgar_fotos(fotos_fin, 'custodia_fin', vigente.id,
                          registrado_por_usuario_id, ahora)

        nueva = Custodia(
            vehiculo_id=vehiculo_id,
            custodio_tipo=custodio_tipo.value,
            custodio_conductor_id=custodio_conductor_id,
            custodio_sede_id=custodio_sede_id,
            custodio_estado=custodio_estado.value,
            registrado_por_usuario_id=registrado_por_usuario_id,
            # MISMO instante que el cierre. Es lo que hace que no exista hueco:
            # no es que el hueco sea corto, es que no existe.
            inicio_ts=ahora,
            km_inicio=km,
            # Arranque en frío: la primera custodia de cada vehículo. Sus daños
            # nacen preexistentes, sin responsable — nadie paga por lo que no
            # sabemos cuándo apareció.
            linea_base=(vigente is None
                        and Custodia.query.filter_by(vehiculo_id=vehiculo_id).count() == 0),
        )
        db.session.add(nueva)
        db.session.flush()

        db.session.add(LecturaOdometro(
            vehiculo_id=vehiculo_id, valor_km=km, ts=ahora,
            origen=OrigenLectura.ENTREGA.value,
            autor_usuario_id=registrado_por_usuario_id,
        ))
        _colgar_fotos(fotos_inicio, 'custodia_inicio', nueva.id,
                      registrado_por_usuario_id, ahora)

        db.session.commit()
        return nueva
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba y
        # volver a levantar. Un traspaso a medias es peor que uno que falló.
        db.session.rollback()
        raise


def _colgar_fotos(fotos, entidad_tipo, entidad_id, autor_id, ahora):
    """Ata cada foto a su padre. Un archivo sin padre es un bug (regla 7)."""
    for f in fotos or []:
        db.session.add(Foto(
            clase=f['clase'], entidad_tipo=entidad_tipo, entidad_id=entidad_id,
            storage_ref=f['storage_ref'], hash_sha256=f['hash_sha256'],
            bytes=f['bytes'], ancho=f['ancho'], alto=f['alto'],
            mime=f['mime'], ts_captura=f['ts_captura'] if 'ts_captura' in f else ahora,
            gps_lat=f['gps_lat'] if 'gps_lat' in f else None,
            gps_lon=f['gps_lon'] if 'gps_lon' in f else None,
            autor_usuario_id=autor_id,
            simulado=f['simulado'] if 'simulado' in f else False,
            estado=f['estado'] if 'estado' in f else 'ok',
        ))


__all__ = ['traspasar', 'custodia_activa', 'FOTOS_POR_CUSTODIA']
