"""
Por dónde entra y sale una llanta. La puerta de `flota_llanta` y
`flota_montaje_llanta`.

Tres verbos, y ninguno más:

```
dar_de_alta  →  flota_llanta                    (la llanta existe, no está puesta)
montar       →  flota_montaje_llanta ABIERTO    (posición ocupada, reloj corriendo)
desmontar    →  flota_montaje_llanta CERRADO    (la vida de ese tramo terminó)
```

Y las lecturas: `expediente_de` (un vehículo), `historia_de` (una llanta),
`km_por_posicion` y `posiciones_libres`.

## Qué NO tiene, y por qué

· **No hay `rotar`.** Una rotación es desmontar y montar, y los dos gestos ya
  existen. Un verbo atómico haría falta si el estado intermedio fuera imposible
  —como en `traspaso.traspasar`, donde un vehículo sin custodia ni un segundo es
  un hueco de cobertura—; acá el estado intermedio es real: el camión está en el
  gato y la posición está vacía. `posiciones_sin_llanta` lo cuenta y lo dice.
· **No hay `dar_de_baja`.** Todavía no se dio de baja una sola llanta. El gesto
  lo define la primera que ocurra de verdad (regla 12); inventarlo antes es
  diseñar contra un caso imaginado. Deuda declarada en `ESTADO.md`.
· **No hay `editar` ni `borrar`.** Un montaje mal registrado se corrige con el
  mismo criterio que un odómetro: registrando el hecho. Hoy no hay una sola fila
  que corregir.
· **Ninguna función recibe conductor.** Regla 2: una llanta que se gastó
  irregularmente es una desalineación, una presión mal llevada o un eje torcido,
  y ninguna de las tres se investiga mejor con un nombre al lado.

## Regla 3 — de dónde salen las dos lecturas

Montar y desmontar son gestos de taller: **alguien está parado al lado del
vehículo y ve el tablero**. Por eso las dos lecturas se piden y no se heredan, a
diferencia de las nueve categorías de gasto «de escritorio». El anclaje es
`hallazgos.anclar_odometro` con `origen=OrigenLectura.OT` —la misma política, no
una copia (regla 0)—, y `ot` es el mismo origen que `gastos.ORIGEN_DE_LECTURA`
ya le asigna a la categoría `llanta`: las dos vías dicen lo mismo sobre el mismo
gesto.

Si el kilometraje coincide con la última lectura, se reutiliza esa fila. Montar
seis llantas en una misma visita al taller cuelga los seis montajes de **una**
lectura, que es lo que de verdad pasó — en vez de fabricar el ruido que
`lecturas_ts_duplicado` existe para contar.

## La confianza del kilómetro viaja hasta la pantalla

`km_del_montaje` recibe las dos lecturas con su `confianza` y la marca la decide
`dominio.odometro.confianza_del_tramo`. Consecuencia medida y no cosmética: como
el formulario de montaje **no pide foto del tablero**, las lecturas nacen
`dudosa` (`confianza_al_nacer`, regla 1) y el km de la llanta sale `sin_dato`
hasta que alguien pase por la cola de verificación. Es lo mismo que le pasa al
CPK desde el 2026-09-02, y es a propósito: un número que nadie puede respaldar no
se publica como firme.
"""
from datetime import datetime
from typing import List, Optional, Union

from app.extensions import db
from app.models.vehiculo import Vehiculo
from flota.adaptadores.hallazgos import anclar_odometro
from flota.adaptadores.modelos import (FichaTecnica, Gasto, Llanta,
                                       MontajeLlanta)
from flota.dominio import llantas as dom
from flota.dominio.errores import ErrorFlota
from flota.dominio.llantas import SIN_DATO, VIGENTE
from flota.dominio.valores import OrigenLectura


class LlantaInvalida(ErrorFlota):
    """La operación sobre la llanta no se puede hacer. No se hace a medias.

    Es `ErrorFlota` y no `ValueError` para que la frontera lo traduzca a 400/409
    en vez de a un 500 — mismo criterio que `HallazgoInvalido` y `GastoInvalido`.
    Un dato malo no es una falla del sistema.
    """


def posiciones_declaradas(vehiculo_id: int) -> Optional[int]:
    """Cuántas posiciones de llanta declara la ficha de ese vehículo.

    `None` si el vehículo no tiene ficha, y **`None` no es cero**: cero
    posiciones sería un vehículo sin ruedas, y lo que pasa es que nadie levantó
    la ficha. `posiciones_llanta` es NOT NULL, así que la fila o dice el número o
    no existe.

    **No cae al fallback por tipo de `dominio.valores.posiciones_llanta()`.** Ese
    fallback existe para decidir cuántas FOTOS pedirle a un conductor a las
    5 a.m. —donde equivocarse cuesta una foto de menos— y no para autorizar un
    dato que después decide una vida útil por posición.
    """
    ficha = FichaTecnica.query.filter_by(vehiculo_id=vehiculo_id).first()
    return None if ficha is None else int(ficha.posiciones_llanta)


def dar_de_alta(*, codigo: str, medida: str, registrada_por_usuario_id: int,
                marca: Optional[str] = None,
                ts: Optional[datetime] = None) -> Llanta:
    """Da de alta una llanta física. **No la monta en nada.**

    QUÉ AFIRMA al devolver: que existe una llanta con ese código, esa medida y
    esa marca, y quién la dio de alta.

    QUÉ NO AFIRMA:

    · **Que esté puesta en algún vehículo.** Una llanta en bodega es un estado
      real y frecuente —es lo que pasa entre que llega la factura y entra el
      camión al taller—, y por eso el alta y el montaje son dos gestos. Que se
      pueda quedar dada de alta sin montar no es un cabo suelto: es el inventario.
    · **Que esté nueva.** No hay columna de vida ni de reencauche: todavía no se
      reencauchó una sola (regla 12).
    · **Que quepa en ningún vehículo.** La medida se guarda; compararla contra
      `flota_ficha_tecnica.medida_llanta` es otra pregunta, y hoy esa columna es
      nullable en la mitad de las fichas.

    El código repetido lo ataja la base (`codigo` es `UNIQUE`) y acá se levanta
    antes con un mensaje legible: dos llantas con el mismo código son dos
    historias mezcladas, y mezcladas no se pueden separar después.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    limpio = (codigo or '').strip()
    if not limpio:
        raise LlantaInvalida(
            'una llanta sin código no se puede seguir: el código es lo único '
            'que la distingue de las otras cinco iguales del mismo camión, y '
            'sin él «¿cuánto duran?» no se puede ni formular.')
    med = (medida or '').strip()
    if not med:
        raise LlantaInvalida(
            'falta la medida: se lee del flanco y es lo que dice en qué '
            'vehículos entra.')
    if Llanta.query.filter_by(codigo=limpio).first() is not None:
        raise LlantaInvalida(
            f'ya existe una llanta con el código {limpio!r}. Dos llantas con el '
            f'mismo código son dos historias mezcladas.')

    try:
        fila = Llanta(codigo=limpio, medida=med,
                      marca=(marca or '').strip() or 'sin_dato',
                      registrada_por_usuario_id=registrada_por_usuario_id,
                      creada_ts=ahora)
        db.session.add(fila)
        db.session.commit()
        return fila
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba.
        db.session.rollback()
        raise


def montar(*, llanta_id: int, vehiculo_id: int, posicion: int, km: int,
           montada_por_usuario_id: int, gasto_id: Optional[int] = None,
           observacion: Optional[str] = None,
           ts: Optional[datetime] = None) -> MontajeLlanta:
    """Pone una llanta en una posición de un vehículo. Abre el tramo.

    QUÉ AFIRMA al devolver: que esa llanta quedó registrada en esa posición de
    ese vehículo desde ese instante, anclada a un kilometraje real.

    QUÉ NO AFIRMA:

    · **Que la llanta esté buena.** No hay inspección acá. Un daño se reporta
      como hallazgo, que es la tabla que tiene reloj.
    · **Que la medida sea la correcta para el vehículo.** Ver `dar_de_alta`.
    · **Nada sobre nadie** (regla 2). `montada_por_usuario_id` es quién lo
      registró, un hecho.

    Juzga **antes** de escribir y no deja nada a medias. Las dos combinaciones
    imposibles —dos llantas en la misma posición, una llanta en dos sitios— se
    comprueban acá para dar un error legible **y** las impiden dos índices únicos
    parciales en la base: un `check-then-insert` sin índice detrás no es un
    invariante, es una carrera. La validación de acá puede perder la carrera; el
    índice no.

    La posición se valida contra la ficha del vehículo con
    `dominio.llantas.posicion_valida`, y **también** por trigger en la base. Un
    vehículo sin ficha no admite montajes: no hay contra qué validar la posición,
    y aceptar por omisión dejaría entrar la posición 9 de un motocarro.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    # ── 1. Juzgar ANTES de escribir ──────────────────────────────────────
    llanta = Llanta.query.get(llanta_id)
    if llanta is None:
        raise LlantaInvalida(f'no existe la llanta {llanta_id}')
    if Vehiculo.query.get(vehiculo_id) is None:
        raise LlantaInvalida(f'no existe el vehículo {vehiculo_id}')

    declaradas = posiciones_declaradas(vehiculo_id)
    if not dom.posicion_valida(posicion, declaradas):
        cuantas = ('no tiene ficha técnica, así que no hay contra qué validar '
                   'la posición' if declaradas is None
                   else f'su ficha declara {declaradas} posiciones')
        raise LlantaInvalida(
            f'la posición {posicion!r} no existe en este vehículo: {cuantas}. '
            f'Una llanta en una posición que no existe deja el kilometraje por '
            f'posición hablando de una rueda imaginaria.')

    ocupada = _vigente_en(vehiculo_id, posicion)
    if ocupada is not None:
        raise LlantaInvalida(
            f'la posición {posicion} de este vehículo ya tiene la llanta '
            f'{ocupada.llanta.codigo!r} montada desde '
            f'{ocupada.inicio_ts:%d/%m/%Y}. Desmontala primero: dos llantas en '
            f'una posición no es un dato raro, es un dato imposible.')

    puesta = montaje_vigente_de(llanta_id)
    if puesta is not None:
        raise LlantaInvalida(
            f'la llanta {llanta.codigo!r} ya está montada en la posición '
            f'{puesta.posicion} de '
            f'{Vehiculo.query.get(puesta.vehiculo_id).placa}. Una llanta no '
            f'puede estar en dos sitios: si se movió, hay que desmontarla de '
            f'donde estaba — si no, sus kilómetros se cuentan dos veces.')

    if gasto_id is not None:
        _validar_gasto(gasto_id, vehiculo_id)

    try:
        lectura = anclar_odometro(vehiculo_id, km, montada_por_usuario_id, ahora,
                                  origen=OrigenLectura.OT)
        fila = MontajeLlanta(
            llanta_id=llanta_id, vehiculo_id=vehiculo_id, posicion=posicion,
            inicio_ts=ahora, lectura_inicio_id=lectura.id,
            montada_por_usuario_id=montada_por_usuario_id,
            gasto_id=gasto_id,
            observacion=(observacion or '').strip() or None,
        )
        db.session.add(fila)
        db.session.commit()
        return fila
    except Exception:
        db.session.rollback()
        raise


def desmontar(*, montaje_id: int, km: int, motivo: str,
              desmontada_por_usuario_id: int,
              observacion: Optional[str] = None,
              ts: Optional[datetime] = None) -> MontajeLlanta:
    """Saca la llanta. Cierra el tramo, con su kilometraje y **su motivo**.

    QUÉ AFIRMA al devolver: que ese montaje terminó en ese instante, con ese
    odómetro, y por el motivo declarado.

    QUÉ NO AFIRMA:

    · **Que la llanta esté para la basura.** `motivo_desmontaje` dice por qué
      salió, no en qué estado quedó. Una `rotacion` sale entera.
    · **Que haya durado poco o mucho.** No hay umbral (regla 13): no hay una sola
      llanta medida en esta flota.

    El motivo es **obligatorio y no tiene default** — el `CHECK` de desenlace lo
    respalda en la base. Es el eje entero del análisis: sin `desgaste_irregular`,
    una llanta que murió por una desalineación y una que cumplió su vida quedan
    indistinguibles, y el eje que come flancos no aparece nunca. `sin_dato` es
    una respuesta legítima y hay que escribirla; lo que no puede pasar es que la
    opción cómoda sea `desgaste_normal`.

    Desmontar dos veces levanta. El segundo cierre movería `fin_ts` hacia
    adelante y cambiaría un número de kilómetros que ya se publicó; el silencio
    («ya estaba desmontada, no pasa nada») haría que un cierre equivocado se
    tapara con otro. Mismo criterio que `hallazgos.cerrar`.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    if motivo not in dom.MOTIVOS_DESMONTAJE:
        raise LlantaInvalida(
            f'motivo de desmontaje desconocido: {motivo!r}. Conocidos: '
            f'{", ".join(dom.MOTIVOS_DESMONTAJE)}. `sin_dato` es una respuesta '
            f'legítima y hay que escribirla — sin ella, la opción cómoda sería '
            f'siempre `desgaste_normal`.')

    fila = MontajeLlanta.query.get(montaje_id)
    if fila is None:
        raise LlantaInvalida(f'no existe el montaje {montaje_id}')
    if fila.fin_ts is not None:
        raise LlantaInvalida(
            f'el montaje {montaje_id} ya se cerró el '
            f'{fila.fin_ts:%d/%m/%Y}. Alguien lo desmontó mientras esta '
            f'pantalla estaba abierta.')

    try:
        lectura = anclar_odometro(fila.vehiculo_id, km,
                                  desmontada_por_usuario_id, ahora,
                                  origen=OrigenLectura.OT)
        fila.fin_ts = ahora
        fila.lectura_fin_id = lectura.id
        fila.motivo_desmontaje = motivo
        fila.desmontada_por_usuario_id = desmontada_por_usuario_id
        if (observacion or '').strip():
            fila.observacion = observacion.strip()
        db.session.commit()
        return fila
    except Exception:
        db.session.rollback()
        raise


# ── Lecturas ─────────────────────────────────────────────────────────────────

def montaje_vigente_de(llanta_id: int) -> Optional[MontajeLlanta]:
    """Dónde está puesta esa llanta ahora mismo, o `None`.

    `None` acá sí significa «no está puesta en ninguna parte»: el índice único
    parcial garantiza que hay cero o uno, así que la ausencia es un hecho medido
    y no un no-sé. Es la misma garantía que `traspaso.custodia_activa`.
    """
    return MontajeLlanta.query.filter(
        MontajeLlanta.llanta_id == llanta_id,
        MontajeLlanta.fin_ts.is_(None)).one_or_none()


def _vigente_en(vehiculo_id: int, posicion: int) -> Optional[MontajeLlanta]:
    """Qué llanta está en esa posición de ese vehículo ahora, o `None`.

    `one_or_none` y no `first`: el índice único parcial garantiza cero o uno, y
    si alguna vez hubiera dos, esto tiene que reventar ruidosamente en vez de
    elegir una — que es cómo `/flota/conductor/mi-turno` se cayó el 2026-08-13,
    y fue la caída la que hizo visible el invariante roto.
    """
    return MontajeLlanta.query.filter(
        MontajeLlanta.vehiculo_id == vehiculo_id,
        MontajeLlanta.posicion == posicion,
        MontajeLlanta.fin_ts.is_(None)).one_or_none()


def _validar_gasto(gasto_id: int, vehiculo_id: int) -> Gasto:
    """El gasto existe y es **de este vehículo**.

    Un montaje colgado del gasto de otro camión no rompe ninguna FK y no se ve
    raro en ninguna pantalla: la plata sigue en la espina y el CPK sigue saliendo
    bien, porque el CPK divide por vehículo y el gasto está donde tiene que
    estar. Lo que se pierde en silencio es la única pregunta que esta referencia
    contesta —*«¿de qué factura salió esta llanta?»*—, y se pierde apuntando a un
    documento que existe: la respuesta se lee con confianza y se lee mal.

    Va acá y no en la base porque un `CHECK` no puede mirar otra fila y un
    trigger más por esto no se justifica: es una referencia de auditoría, no un
    invariante del que dependa un cálculo. Queda declarado en `ESTADO.md`.
    """
    gasto = Gasto.query.get(gasto_id)
    if gasto is None:
        raise LlantaInvalida(f'no existe el gasto {gasto_id}')
    if gasto.vehiculo_id != vehiculo_id:
        raise LlantaInvalida(
            f'el gasto {gasto_id} es de otro vehículo. Un montaje colgado de la '
            f'factura del camión equivocado no da error en ninguna parte y deja '
            f'la trazabilidad apuntando a un documento real que no es el suyo.')
    return gasto


def tramo_de(montaje: MontajeLlanta):
    """`(km, marca)` de un montaje. El juicio es del dominio, no de acá.

    Una sola política: `dominio.llantas.km_del_montaje`, que a su vez usa
    `dominio.odometro.confianza_del_tramo`. Escribir acá una resta y un
    `if confianza == ...` sería la segunda copia, y la copia de la frontera es la
    que decide si un número se publica.
    """
    return dom.km_del_montaje(
        lectura_inicio=montaje.lectura_inicio.a_dominio(),
        lectura_fin=(montaje.lectura_fin.a_dominio()
                     if montaje.lectura_fin is not None else None))


def km_de_llanta(llanta_id: int):
    """Lo que lleva rodado una llanta, **calculado y no guardado**.

    Devuelve `(valor, marca)` con `valor` en `int | VIGENTE | SIN_DATO`. Ver
    `dominio.llantas.km_acumulado` para qué afirma cada uno — y ninguno es cero.
    """
    montajes = MontajeLlanta.query.filter_by(llanta_id=llanta_id).all()
    return dom.km_acumulado([tramo_de(m) for m in
                             sorted(montajes, key=lambda m: (m.inicio_ts, m.id))])


def montajes_de(vehiculo_id: int) -> List[MontajeLlanta]:
    """Todos los montajes del vehículo: el vigente de cada posición y su historia.

    Ordenados por posición y, dentro de cada una, del más reciente al más viejo —
    que es el orden en que alguien lee un expediente. El desempate por `id` deja
    la lista estable: dos montajes del mismo segundo existen (seis llantas en una
    visita al taller), y sin desempate el orden cambiaría entre consultas.
    """
    filas = MontajeLlanta.query.filter_by(vehiculo_id=vehiculo_id).all()
    return sorted(filas, key=lambda m: (m.posicion, -m.inicio_ts.timestamp(),
                                        -m.id))


def historia_de(llanta_id: int) -> List[MontajeLlanta]:
    """Todos los montajes de una llanta, del más viejo al más nuevo.

    Es la vista que solo existe porque la llanta es una entidad: la misma llanta
    en tres posiciones de dos camiones a lo largo de un año. Como renglón de
    factura, esta lista no se podría armar.
    """
    filas = MontajeLlanta.query.filter_by(llanta_id=llanta_id).all()
    return sorted(filas, key=lambda m: (m.inicio_ts, m.id))


def posiciones_libres(vehiculo_id: int) -> Union[List[int], str]:
    """Las posiciones del vehículo que hoy no tienen ninguna llanta registrada.

    `SIN_DATO` si el vehículo no tiene ficha — jamás `[]`. Ver
    `dominio.llantas.posiciones_sin_llanta`: `[]` significaría «se revisó y están
    todas cubiertas», y lo que pasa es que no se sabe cuántas hay.

    **Casi siempre significa que la llanta está puesta y nadie la registró**, no
    que el camión ande sin rueda. Es la medida de cuánto le falta al inventario
    para describir el vehículo real.
    """
    ocupadas = [m.posicion for m in MontajeLlanta.query.filter(
        MontajeLlanta.vehiculo_id == vehiculo_id,
        MontajeLlanta.fin_ts.is_(None)).all()]
    return dom.posiciones_sin_llanta(posiciones_declaradas(vehiculo_id),
                                     ocupadas)


def km_por_posicion(vehiculo_id: int) -> List[dict]:
    """El hecho medido por posición de ESE vehículo, con lo que falta para
    poder fijar una vida útil.

    Solo entran los montajes **cerrados y con kilometraje publicable**: una vida
    a medias no es una vida, y un tramo entre dos lecturas dudosas no es un
    número (`SIN_DATO`). Los excluidos no se rellenan con nada.

    Por vehículo y por posición, **nunca agregado sobre la flota**: una
    direccional de un NHR y la de un motocarro no duran lo mismo y el promedio
    mediría la composición del parque, que es el mismo error que el canon del CPK
    prohíbe expresamente.
    """
    por_posicion = {}
    for m in MontajeLlanta.query.filter(
            MontajeLlanta.vehiculo_id == vehiculo_id,
            MontajeLlanta.fin_ts.isnot(None)).all():
        km, _marca = tramo_de(m)
        if km == VIGENTE or km == SIN_DATO:
            continue
        por_posicion.setdefault(m.posicion, []).append(km)
    return dom.vida_util_por_posicion(por_posicion)


def expediente_de(vehiculo_id: int) -> dict:
    """Todo lo que la pantalla de un vehículo necesita saber de sus llantas.

    Una sola función y no cuatro llamadas sueltas desde la frontera: la vista
    tiene que poder decir, para la misma foto de la base, qué hay puesto, qué
    falta y qué se midió. Armada en tres consultas desde la frontera, un montaje
    registrado en el medio dejaría una posición contada como ocupada y libre a la
    vez.
    """
    return {
        'posiciones_declaradas': posiciones_declaradas(vehiculo_id),
        'montajes': montajes_de(vehiculo_id),
        'libres': posiciones_libres(vehiculo_id),
        'km_por_posicion': km_por_posicion(vehiculo_id),
    }


def sin_montar() -> List[Llanta]:
    """Las llantas dadas de alta que hoy no están puestas en ningún vehículo.

    Es lo que el formulario de montaje ofrece. **No afirma que estén buenas**:
    una llanta desmontada por `corte_flanco` sigue apareciendo acá porque no hay
    baja todavía (regla 12), y el desplegable muestra el último motivo de
    desmontaje al lado para que quien elige lo vea.
    """
    puestas = {m.llanta_id for m in MontajeLlanta.query.filter(
        MontajeLlanta.fin_ts.is_(None)).all()}
    return [ll for ll in Llanta.query.order_by(Llanta.codigo).all()
            if ll.id not in puestas]


__all__ = ['dar_de_alta', 'montar', 'desmontar', 'montajes_de', 'historia_de',
           'km_de_llanta', 'km_por_posicion', 'posiciones_libres',
           'posiciones_declaradas', 'montaje_vigente_de', 'expediente_de',
           'tramo_de',
           'sin_montar', 'LlantaInvalida', 'VIGENTE', 'SIN_DATO']
