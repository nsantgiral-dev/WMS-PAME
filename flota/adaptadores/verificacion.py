"""
La cola de verificación — donde una persona convierte un número en un dato.

`confianza` se escribe al nacer la lectura y nunca dice `verificada`: ningún
automatismo puede escribir esa palabra (lo impone el trigger
`flota_odometro_nace_no_verificada`, no solo el código). Este adaptador es la
**única** vía por la que aparece, y detrás de ella hay un humano mirando la foto
del tablero con el número al lado.

```
dudosa ──── confirmar ────→ verificada     (queda quién y cuándo)
   │
   └─────── corregir ─────→ una lectura NUEVA con origen=correccion
                            (la tabla es append-only: el número no se edita)
```

**Corregir no vive acá y es a propósito.** Ya existe una sola puerta para eso —
`POST /flota/odometro` con `origen=correccion`, que exige motivo escrito— y una
segunda que hiciera lo mismo desde la cola sería la política escrita dos veces.
Lo que sí vive acá es la consecuencia: una lectura que una corrección ANULÓ
(o, con las correcciones viejas, dejó atrás) **sale de la cola**, porque pedirle a alguien que confirme un número
que el sistema ya reemplazó es hacerle perder el tiempo con lo que no decide
nada. La ventana la contesta el dominio
(`vigentes_tras_la_ultima_correccion`), no un `WHERE` escrito acá.

## Por qué esto no es la regla 12 rota

La regla 12 pide uso real de lo anterior antes de construir lo siguiente, y una
pantalla que nace vacía es exactamente lo que prohíbe. **Ésta nace con
contenido, y está medido**: `medicion.lecturas_sin_foto` cuenta las lecturas con
`foto_id IS NULL`, y en producción eran **26 de 26** el 2026-09-01. La regla 1 de
`confianza_al_nacer` marca `dudosa` toda lectura sin foto, así que la cola tiene
esas 26 desde el primer día — no es una pantalla esperando un caso hipotético,
es la pantalla del estado actual de la flota.

Lo que la cola NO afirma sobre esas 26: que estén mal. Casi con seguridad son
correctas; lo que no tienen es con qué demostrarlo.
"""
from datetime import datetime
from typing import List, Optional, Tuple

from app.extensions import db
from app.models.vehiculo import Vehiculo
from flota.adaptadores.modelos import LecturaOdometro
from flota.dominio.errores import ErrorFlota
from flota.dominio.odometro import vigentes_tras_la_ultima_correccion
from flota.dominio.valores import Confianza


class VerificacionInvalida(ErrorFlota):
    """No se puede verificar esa lectura. No se verifica a medias."""


def pendientes() -> List[Tuple[LecturaOdometro, str]]:
    """Las lecturas dudosas que todavía deciden algo, con la placa de su vehículo.

    QUÉ AFIRMA: que cada fila devuelta está marcada `dudosa`, que su motivo
    escrito dice por qué, y que ninguna corrección posterior de ese mismo
    vehículo la dejó atrás.

    QUÉ NO AFIRMA: que el número esté mal. `dudosa` es «no la uses para dividir
    hasta que alguien la mire».

    Orden: **la más vieja primero**. Es la que lleva más tiempo sin dejar
    calcular nada aguas abajo, y la que un CPK de un mes cerrado ya no va a
    poder recuperar. El empate se rompe por `id` para que la lista sea estable
    entre dos cargas de la pantalla.
    """
    filas = (db.session.query(LecturaOdometro, Vehiculo.placa)
             .join(Vehiculo, Vehiculo.id == LecturaOdometro.vehiculo_id)
             .filter(LecturaOdometro.confianza == Confianza.DUDOSA.value)
             .all())
    if not filas:
        return []

    # La ventana de corrección se pregunta POR VEHÍCULO y con la serie completa:
    # una corrección es una fila más de la misma tabla, y el dominio la juzga
    # contra todas las previas del vehículo, no solo contra las dudosas.
    #
    # De la respuesta se usa `desde` —el instante en que empieza la ventana— y
    # no la lista: comparar filas contra objetos de dominio obligaría a
    # emparejarlas por valor, y dos lecturas del mismo segundo con el mismo
    # kilometraje (las hay: `lecturas_ts_duplicado` cuenta diez) no se
    # distinguen así. El instante sí es exacto.
    #
    # Desde el 2026-09-27 la respuesta trae los ids (`a_dominio` los pasa), y
    # la lista ya excluye las lecturas que una corrección ANULÓ: se cruza por
    # id. Un salto o una tardía sin verificar SÍ quedan en la cola: verificar
    # un salto es justamente lo que lo hace contar.
    vigentes_por_vehiculo = {}
    for vehiculo_id in {l.vehiculo_id for l, _ in filas}:
        serie = LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id).all()
        vigentes, _desde = vigentes_tras_la_ultima_correccion(
            [l.a_dominio() for l in serie])
        vigentes_por_vehiculo[vehiculo_id] = {l.id for l in vigentes}

    return sorted(
        [(l, placa) for l, placa in filas
         if l.id in vigentes_por_vehiculo[l.vehiculo_id]],
        key=lambda par: (par[0].ts, par[0].id))


def _exigir_que_la_serie_siga_creciendo(fila) -> None:
    """Verificar un salto o una lectura que contradice la HACE contar
    (2026-09-29, validación): si con ella la serie queda decreciente —un
    125.000 con lecturas de 12.600 después, o un 12.520 con un 13.500 antes
    que sigue contando—, el tope saltaría y el camión se trabaría otra vez.
    Primero se anula la equivocada; después se verifica."""
    from flota.dominio import odometro as dom_odo

    if fila.serie not in (dom_odo.SERIE_SALTO, dom_odo.SERIE_CONTRADICE):
        return
    serie = LecturaOdometro.query.filter_by(vehiculo_id=fila.vehiculo_id).all()
    cuentan = [l for l in dom_odo.lecturas_que_cuentan([l.a_dominio() for l in serie])
               if l.id != fila.id]
    choca = next((l for l in cuentan
                  if (l.ts < fila.ts and l.valor_km > fila.valor_km)
                  or (l.ts > fila.ts and l.valor_km < fila.valor_km)), None)
    if choca is not None:
        raise VerificacionInvalida(
            f'verificar la lectura de {fila.valor_km} km dejaría la serie '
            f'decreciente: choca con la de {choca.valor_km} km (lectura '
            f'{choca.id}), que sigue contando. Anule primero la equivocada.')


def verificar(*, lectura_id: int, usuario_id: int,
              ts: Optional[datetime] = None) -> LecturaOdometro:
    """Una persona miró la foto y dijo que el número es ése. Deja su nombre.

    QUÉ AFIRMA la fila que devuelve: que `verificada_por_usuario_id` afirmó, en
    `verificada_ts`, que ese kilometraje es el que muestra el tablero.

    QUÉ NO AFIRMA: que el número sea cierto. Afirma que alguien con nombre lo
    cotejó y se hizo responsable — que es todo lo que un dato puede afirmar y
    exactamente lo que hoy no existe en ninguna de las 26 lecturas.

    **Solo se verifica una `dudosa`.** Una `declarada` no está en la cola y
    nadie la puso en duda: convertirla en `verificada` sin haber mirado nada
    sería inflar la única marca que afirma que alguien miró. Y una `verificada`
    no se re-verifica: mover la fecha de una afirmación ya publicada tapa el
    primer nombre con el segundo, y el registro dejaría de decir quién decidió.

    El `UPDATE` lo permite el trigger `flota_odometro_no_update` **solo para
    esto**: toda otra columna tiene que quedar idéntica y la confianza solo
    puede ir hacia `verificada`. La tabla sigue siendo append-only para el
    número; lo que se escribe acá no es el número.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    fila = db.session.get(LecturaOdometro, lectura_id)
    if fila is None:
        raise VerificacionInvalida(f'no existe la lectura {lectura_id}')

    if fila.confianza == Confianza.VERIFICADA.value:
        raise VerificacionInvalida(
            f'la lectura {lectura_id} ya la verificó el usuario '
            f'{fila.verificada_por_usuario_id}. Verificarla otra vez movería la '
            f'fecha de una afirmación que ya se publicó y taparía el primer '
            f'nombre con el segundo.'
        )
    if fila.confianza != Confianza.DUDOSA.value:
        raise VerificacionInvalida(
            f'la lectura {lectura_id} está {fila.confianza}, no dudosa: nadie '
            f'la puso en duda, así que no hay nada que confirmar. La cola solo '
            f'trae las que tienen un motivo escrito para desconfiar.'
        )
    _exigir_que_la_serie_siga_creciendo(fila)
    anuladora = (LecturaOdometro.query
                 .filter_by(anula_lectura_id=fila.id).first())
    if anuladora is not None:
        raise VerificacionInvalida(
            f'la lectura {lectura_id} la anuló una corrección (lectura '
            f'{anuladora.id}): ya no decide nada, y verificarla no la vuelve a '
            f'contar.'
        )

    try:
        fila.confianza = Confianza.VERIFICADA.value
        fila.verificada_por_usuario_id = usuario_id
        fila.verificada_ts = ahora
        db.session.commit()
        return fila
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba.
        # Una verificación a medias —la marca puesta y el nombre no— es
        # justamente lo que el CHECK `ck_flota_verificada_con_autor` impide.
        db.session.rollback()
        raise


__all__ = ['pendientes', 'verificar', 'VerificacionInvalida']
