"""
Por dónde entra la plata que sale. La puerta de `flota_gasto` y `flota_tanqueo`.

`flota/dominio/costos.py` tiene el cálculo completo y su canon
(`docs/flota/canones/costo_por_kilometro.md`) — y hasta hoy **cero callers**. Es
el mismo patrón que este módulo lleva pagando toda la semana: política escrita,
probada y desplegada, sin nada sobre qué pronunciarse.

Dos verbos, y el segundo es una especialización del primero:

```
registrar_gasto     →  flota_gasto
registrar_tanqueo   →  flota_gasto + flota_tanqueo   (una transacción)
```

Y una lectura: `cpk_de`, que es la ÚNICA consulta del CPK en el repo. El canon
lo pide escrito así —*«vive en una función con nombre y no dentro de la consulta
del tablero»*— porque la copia que se queda dentro del SQL es la que nadie
encuentra cuando la regla cambia, y es la que la gente mira.

## Regla 3 — de dónde sale la lectura, y por qué no todas las categorías la traen

`lectura_id` es NOT NULL: un gasto sin kilometraje no se puede dividir entre
kilómetros, que es lo único para lo que existe. La política de anclaje es
**`hallazgos.anclar_odometro`, no una copia** (regla 0 del WMS): reutiliza la
última lectura si el kilometraje coincide, en vez de fabricar el ruido que
`lecturas_ts_duplicado` existe para contar.

Lo que no se puede hacer es **inventarle un origen a la lectura**. `origen` tiene
un CHECK en la base con siete valores, y ninguno significa «pagué el SOAT en una
oficina». Escribir `ot` en la lectura de un impuesto vehicular sería una fila que
dice de dónde vino y miente — la columna existe justamente para no adivinarlo, y
ensanchar el vocabulario es una migración que esta fase no hace.

Así que las categorías se parten en dos, **por lo que de verdad pasa cuando se
gastan**:

| | Categorías | Qué pasa | Kilometraje |
|---|---|---|---|
| **De campo** | `combustible` → `tanqueo`; `mantenimiento`, `repuesto`, `llanta` → `ot` | Alguien está parado al lado del vehículo y ve el tablero | **Se pide**, y puede nacer una lectura |
| **De escritorio** | las otras nueve | Se paga en una oficina, sin el vehículo delante | **No se pide**: el gasto se cuelga de la última lectura conocida |

Un gasto de escritorio sobre un vehículo **sin ninguna lectura** no se registra:
levanta. No es una molestia burocrática — es la regla 3 diciendo que ese gasto no
va a poder entrar a ningún CPK, y guardarlo igual sería guardar una fila que
nadie va a poder dividir.

`anclar_odometro` ya declara que la lectura que devuelve **no afirma haberse
tomado para este evento**. Esta es exactamente esa licencia, usada a propósito y
no de contrabando.

## Las dos preguntas que el dueño no contestó, y cómo se aguantan las dos

Diseñado agnóstico, y declarado acá porque un supuesto que solo vive en la cabeza
de quien escribió el código se pierde en la primera rotación:

1. **¿Tanquean con tarjeta/convenio o con efectivo del conductor?** `origen_costo`
   es obligatorio y se escribe con palabras (`tarjeta_convenio`,
   `credito_proveedor`, `efectivo_conductor`, `sin_dato`). Si la respuesta es
   efectivo, cada registro es además una legalización de gasto y **quién aprueba
   cambia** — hoy nadie aprueba dentro del WMS, y el día que haga falta la
   columna ya distingue los casos sin migrar. `sin_dato` está en el vocabulario y
   **no es el default**: hay que escribirlo.
2. **¿Contabilidad causa con placa o con centro de costo por vehículo?**
   `documento_numero` (la referencia a Siesa) y `centro_op` son los dos
   nullables y los dos se cuentan en el health. Si causan con placa, esto podría
   leerse de Siesa en vez de digitarse, y `documento_numero` pasa de ser un campo
   que alguien teclea a ser una clave de cruce. La espina aguanta las dos
   respuestas; lo que cambia es quién llena el campo, no el esquema.

Ninguna de las dos se adivina acá. Lo que sí se hace es **contar cuántos gastos
llegan sin documento**, que es el número que contesta si la operación está
entregando las facturas.

## Lo que este adaptador NO hace

· **No bloquea un tanqueo que excede la capacidad del tanque.** Lo registra y lo
  declara. Es la secuencia obligatoria del módulo —medir, corregir, imponer—:
  si el primer día la app rechaza un tanqueo por un campo mal levantado, la
  operación desmonta el sistema en 48 horas. El detector vive en el health y en
  la respuesta del endpoint.
· **No nombra a nadie.** `registrado_por_usuario_id` es quién tecleó, un hecho.
  Ninguna columna de conductor, ninguna de custodia: la regla 2 escrita en el
  esquema. Lo que un tanqueo excedido afirma es que **dos datos no pueden ser
  los dos ciertos** —la capacidad de la ficha o los galones registrados—, y las
  cuatro explicaciones posibles se investigan igual de rápido.
· **No aprueba nada.** La aprobación real es la causación en Siesa. Un segundo
  «aprobado» acá sería un estado que contradice al ERP sin poder corregirlo.
"""
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import List, Optional, Union

from app.extensions import db
from flota.adaptadores.hallazgos import anclar_odometro
from flota.adaptadores.modelos import (FichaTecnica, Gasto, LecturaOdometro,
                                       Tanqueo)
from flota.dominio import costos
from flota.dominio import odometro as dom_odo
from flota.dominio.costos import SIN_DATO
from flota.dominio.errores import ErrorFlota, PermisoInsuficiente
from flota.dominio.valores import Confianza, OrigenLectura


class GastoInvalido(ErrorFlota):
    """El gasto no se puede registrar. No se registra a medias.

    Es `ErrorFlota` y no `ValueError` para que la frontera lo traduzca a 400/409
    en vez de a un 500 — mismo criterio que `HallazgoInvalido`. Un dato malo no
    es una falla del sistema.
    """


#: Qué gesto operativo produce la lectura de cada categoría de campo.
#:
#: **Es un mapa parcial a propósito y se consulta con `in`, nunca con
#: `.get(cat, X)`** (regla 5). Lo que está afuera no es «lo demás»: es la lista
#: de categorías de escritorio, y la diferencia decide si se pide kilometraje.
#:
#: Los tres valores son honestos: `tanqueo` es literalmente un tanqueo, y
#: mantenimiento, repuesto y llanta son lo que pasa cuando hay una orden de
#: trabajo. Ninguna categoría se mapea a un origen que no la describa.
ORIGEN_DE_LECTURA = {
    'combustible':   OrigenLectura.TANQUEO,
    'mantenimiento': OrigenLectura.OT,
    'repuesto':      OrigenLectura.OT,
    'llanta':        OrigenLectura.OT,
}


def es_de_campo(categoria: str) -> bool:
    """¿Este gasto ocurre con el vehículo delante?

    Total sobre el vocabulario, igual que `costos.exige_periodo` y por el mismo
    motivo: una categoría desconocida tiene que reventar acá y no producir un
    gasto de escritorio por omisión.
    """
    if categoria not in costos.CATEGORIAS_GASTO:
        raise GastoInvalido(
            f'categoría de gasto desconocida: {categoria!r}. '
            f'Conocidas: {", ".join(costos.CATEGORIAS_GASTO)}.'
        )
    return categoria in ORIGEN_DE_LECTURA


def _decimal(valor, campo: str) -> Decimal:
    """A `Decimal`, o levanta con el nombre del campo.

    Sin `try: float(...) except: 0`. Un valor ilegible que se guarda como cero es
    una fila que baja el CPK del vehículo y no se ve rara en ninguna pantalla.
    """
    try:
        d = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError):
        raise GastoInvalido(f'{campo} no es un número: {valor!r}')
    if d != d or d.is_infinite():       # NaN o infinito
        raise GastoInvalido(f'{campo} no es un número: {valor!r}')
    return d


def _periodo(categoria: str, fecha: date,
             desde: Optional[date], hasta: Optional[date]):
    """Qué período cubre este gasto. **Lo decide la categoría, no una casilla.**

    Regla 11: si fuera una casilla, el camino barato sería no marcarla y el SOAT
    entero caería sobre un día. `costos.exige_periodo` es quien contesta, y es la
    única implementación de esa pregunta.

    Mandar un período sobre una categoría que no lo cubre **levanta**, no se
    ignora. Ignorarlo es peor que un 400: quien lo mandó se queda creyendo que el
    reparto ocurrió. Es el mismo criterio que la fecha límite del hallazgo, que
    ni se mira porque el cuerpo no la puede traer.
    """
    if costos.exige_periodo(categoria):
        if desde is None or hasta is None:
            raise GastoInvalido(
                f'{categoria} cubre un período y hay que declararlo: sin '
                f'periodo_desde y periodo_hasta, el gasto entero caería sobre '
                f'el día en que se pagó.'
            )
        if hasta < desde:
            raise GastoInvalido(
                f'período invertido: {hasta} termina antes de {desde}.')
        return desde, hasta

    if desde is not None or hasta is not None:
        raise GastoInvalido(
            f'{categoria} se consume el día en que ocurre y no admite período. '
            f'Las categorías que sí lo cubren son: '
            f'{", ".join(costos.CATEGORIAS_CON_PERIODO)}.'
        )
    return fecha, fecha


def _lectura_para(*, vehiculo_id: int, categoria: str, km: Optional[int],
                  usuario_id: int, ahora: datetime,
                  lectura_id: Optional[int] = None) -> LecturaOdometro:
    """La lectura de la que cuelga el gasto. Ver el encabezado del módulo.

    De campo: se exige `km` y se ancla con la política de `hallazgos`.
    De escritorio: se prohíbe `km` y se toma la última lectura del vehículo.

    ## La tercera rama: `lectura_id` — un ancla MEJOR que las otras dos

    Agregada el 2026-09-02 con la fase 2, y **por un hueco que apareció al
    construirla, no al planearla**: `mantenimiento`, `repuesto` y `llanta` están
    clasificadas de campo porque «alguien está parado al lado del vehículo». Eso
    era cierto mientras el gasto de taller fuera la única huella del taller. Con
    `flota_orden_trabajo` existiendo deja de serlo: **la factura del taller se
    digita treinta días después, en una oficina, sin el vehículo delante.**

    Pedirle el kilometraje a quien la teclea da una de dos cosas, las dos malas:
    un número inventado, o el kilometraje del día del trabajo — que
    `anclar_odometro` **rechaza** por retroceso de odómetro, porque el camión
    siguió rodando.

    Así que el llamador puede declarar la lectura exacta a la que colgar el
    gasto. No es un rodeo alrededor de la regla 3: es la regla 3 con un ancla
    mejor que cualquiera de las otras dos ramas —el odómetro **al que se hizo el
    trabajo**— y es la licencia que `anclar_odometro` ya declara: la lectura *no
    afirma haberse tomado para este evento*.

    Lo que sí se verifica, porque un ancla equivocada es peor que ninguna:

    · **La lectura tiene que existir.** Sin `.get`, sin `or None`.
    · **Tiene que ser del mismo vehículo.** Colgar un gasto del odómetro de otro
      camión mete pesos en el numerador de un CPK y kilómetros en el denominador
      de otro, y ninguna de las dos cifras se ve rara.
    · **No se combina con `km`.** Dos formas de decir de dónde sale la lectura
      en la misma llamada es una que se va a ignorar en silencio.
    """
    if lectura_id is not None:
        if km is not None:
            raise GastoInvalido(
                'no se puede mandar `km` y `lectura_id` a la vez: son dos formas '
                'de decir de dónde sale el kilometraje, y una de las dos se '
                'ignoraría sin que nadie se entere.')
        lectura = LecturaOdometro.query.get(lectura_id)
        if lectura is None:
            raise GastoInvalido(f'no existe la lectura de odómetro {lectura_id}')
        if lectura.vehiculo_id != vehiculo_id:
            raise GastoInvalido(
                f'la lectura {lectura_id} es de otro vehículo. Un gasto colgado '
                f'del odómetro de otro camión mete pesos en el numerador de un '
                f'costo por kilómetro y kilómetros en el denominador de otro, y '
                f'ninguna de las dos cifras se ve rara.')
        return lectura

    if es_de_campo(categoria):
        if km is None:
            raise GastoInvalido(
                f'{categoria} se registra con el vehículo delante: el '
                f'kilometraje es obligatorio (regla 3).'
            )
        return anclar_odometro(vehiculo_id, km, usuario_id, ahora,
                               origen=ORIGEN_DE_LECTURA[categoria])

    if km is not None:
        raise GastoInvalido(
            f'{categoria} se paga sin el vehículo delante y no lleva '
            f'kilometraje propio: se cuelga del último ya registrado. Para '
            f'mover el odómetro está su propia pantalla, que pregunta de qué '
            f'gesto salió la lectura.'
        )
    ultima = _ultima_lectura(vehiculo_id)
    if ultima is None:
        raise GastoInvalido(
            'este vehículo no tiene ninguna lectura de odómetro, así que el '
            'gasto no podría entrar a ningún costo por kilómetro (regla 3). '
            'Registrá primero el kilometraje.'
        )
    return ultima


def _ultima_lectura(vehiculo_id: int) -> Optional[LecturaOdometro]:
    """La lectura más reciente del vehículo, desempatando por `id`.

    Mismo desempate que `anclar_odometro`: con dos lecturas del mismo segundo
    —que las hay, `lecturas_ts_duplicado` las cuenta— el orden por `ts` solo es
    ambiguo, y una consulta ambigua devuelve una fila distinta cada vez.
    """
    filas = LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id).all()
    return max(filas, key=lambda l: (l.ts, l.id), default=None)


def _dia_operativo_de(ts: datetime) -> date:
    """El día de Bogotá al que pertenece un timestamp guardado en UTC.

    QUÉ AFIRMA: que un turno cerrado a las 8 p.m. de Colombia cuenta en el día
    en que el conductor lo cerró, no en el siguiente.

    QUÉ NO AFIRMA: nada sobre la confianza de la lectura. Son dos preguntas —
    «¿el número es creíble?» y «¿de qué día es?»— y hasta el 2026-09-02 la
    segunda no la contestaba nadie.

    Una sola implementación, y consume `TZ_BOGOTA` de `app/utils/fecha.py`: la
    regla 5 del WMS ya existía, ya tenía helper y ya tenía tests, y se aplicaba
    en 4 de 16 sitios. Este es el sitio 17.
    """
    from app.utils.fecha import TZ_BOGOTA

    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()


def _exigir_autoridad(categoria: str, usuario_id: int) -> None:
    """Levanta si este usuario no puede registrar esta categoría.

    QUÉ AFIRMA al no levantar: que el rol REAL del usuario —el de su fila, no
    el que vino en el request— admite esta categoría.

    QUÉ NO AFIRMA: que el gasto esté aprobado. Nadie aprueba gastos dentro del
    WMS en esta fase, y FLO-PR-01 dice que `control_flota` no aprueba.

    **La autoridad se resuelve contra la base, no por parámetro.** Un guard cuya
    precondición la manda quien llama no es un guard — es el defecto gemelo que
    la auditoría del 2026-09-02 encontró en `traspaso.py`, donde `mismo_custodio`
    salía del cuerpo del request.

    Un usuario inexistente o inactivo **no pasa**, y una categoría desconocida
    exige maestros: el lado conservador (regla 0). Ningún `.get(x, default)` —
    un rol que no se reconoce no degrada al más amplio, que es exactamente cómo
    se cuela una escalada.
    """
    from app.models.usuario import Usuario
    from app.routes._auth_helpers import Roles

    if not costos.exige_maestros(categoria):
        return

    permitidos = tuple(Roles.GESTION) + (Roles.CONTROL_FLOTA,)
    u = Usuario.query.get(usuario_id)
    if u is None or not u.activo or u.rol not in permitidos:
        raise PermisoInsuficiente(
            f'la categoría «{categoria}» la registra gestión o control de '
            f'flota, no el rol «{u.rol if u is not None else "desconocido"}». '
            f'En campo se registra: {", ".join(costos.CATEGORIAS_DE_CAMPO)}.'
        )


def registrar_gasto(
    *,
    vehiculo_id: int,
    categoria: str,
    fecha: date,
    valor,
    proveedor: str,
    origen_costo: str,
    registrado_por_usuario_id: int,
    km: Optional[int] = None,
    lectura_id: Optional[int] = None,
    documento_numero: Optional[str] = None,
    centro_op: Optional[str] = None,
    descripcion: Optional[str] = None,
    periodo_desde: Optional[date] = None,
    periodo_hasta: Optional[date] = None,
    ts: Optional[datetime] = None,
    commit: bool = True,
) -> Gasto:
    """Registra un peso que salió por un vehículo.

    QUÉ AFIRMA al devolver: que el gasto quedó escrito con su categoría, su
    valor, el período que cubre, el kilometraje al que se ancla y quién lo
    registró.

    QUÉ NO AFIRMA:

    · **Que esté causado.** El valor, el proveedor, el impuesto y la causación
      viven en Siesa; acá el `valor` existe para poder dividirlo entre
      kilómetros. Un número de esta tabla no cuadra con un balance.
    · **Que alguien lo haya aprobado.** Nadie aprueba gastos dentro del WMS en
      esta fase, y `control_flota` tiene escrito en el procedimiento FLO-PR-01
      que no los aprueba.
    · **Que sea responsabilidad de nadie.** `registrado_por_usuario_id` es quién
      tecleó (regla 2).

    Juzga **antes** de escribir y no deja nada a medias: si algo revienta, la
    base queda como estaba. `commit=False` existe para que `registrar_tanqueo`
    escriba el gasto y su extremidad dentro de la misma transacción — y en ese
    modo **el que abrió la transacción la cierra**, igual que en `hallazgos`.

    La factura repetida la ataja la base (`uq_flota_gasto_documento`), no esto:
    la vía típica de la doble carga es un reintento del formulario, y para
    entonces el adaptador ya devolvió.

    `lectura_id` es la tercera forma de contestar la regla 3 y **la usa un solo
    llamador**: `taller.registrar_factura`, que conoce el odómetro al que se hizo
    el trabajo y no puede pedírselo a quien digita la factura treinta días
    después. Ver `_lectura_para` para qué se verifica y por qué no es un rodeo.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    # ── 0. La autoridad, antes que el dato ───────────────────────────────
    #
    # Va acá y no en la ruta porque `registrar_tanqueo` es una SEGUNDA PUERTA a
    # esta misma operación: hasta el 2026-09-02 pedía `LECTURA_FLOTA` mientras
    # `POST /flota/gastos` pedía `MAESTROS_FLOTA`, así que un conductor escribía
    # en la tabla de la que sale el CPK y no podía leer lo que acababa de
    # escribir. Es la forma de `/liquidar-completo`. Un guard en la ruta protege
    # esa ruta; uno en el servicio protege la operación (lección de packing).
    _exigir_autoridad(categoria, registrado_por_usuario_id)

    # ── 1. Juzgar ANTES de escribir ──────────────────────────────────────
    if not (proveedor or '').strip():
        raise GastoInvalido(
            'un gasto sin proveedor no se puede cruzar con nada: quien lo '
            'audite mañana no va a saber a quién se le pagó.')
    if origen_costo not in costos.ORIGENES_COSTO:
        raise GastoInvalido(
            f'origen de costo desconocido: {origen_costo!r}. Conocidas: '
            f'{", ".join(costos.ORIGENES_COSTO)}. `sin_dato` es una respuesta '
            f'legítima y hay que escribirla.')

    monto = _decimal(valor, 'valor')
    if monto <= 0:
        raise GastoInvalido(
            f'un gasto de {monto} no es un gasto barato: es una fila que '
            f'alguien guardó sin saber el valor.')

    # `es_de_campo` valida la categoría contra el vocabulario y levanta si no
    # existe. Se llama acá arriba para que un `categoria` inventado falle antes
    # de tocar la ficha o el odómetro.
    es_de_campo(categoria)
    desde, hasta = _periodo(categoria, fecha, periodo_desde, periodo_hasta)

    if categoria == 'otro' and not (descripcion or '').strip():
        raise GastoInvalido(
            '`otro` exige escribir qué fue. Es la válvula del catálogo cerrado '
            'y no puede ser la salida cómoda: sin descripción, el desglose por '
            'categoría deja de decir nada.')

    try:
        lectura = _lectura_para(vehiculo_id=vehiculo_id, categoria=categoria,
                                km=km, usuario_id=registrado_por_usuario_id,
                                ahora=ahora, lectura_id=lectura_id)

        fila = Gasto(
            vehiculo_id=vehiculo_id,
            categoria=categoria,
            fecha=fecha,
            valor=monto,
            lectura_id=lectura.id,
            proveedor=proveedor.strip(),
            documento_numero=_texto_o_none(documento_numero),
            periodo_desde=desde,
            periodo_hasta=hasta,
            centro_op=_texto_o_none(centro_op),
            origen_costo=origen_costo,
            descripcion=_texto_o_none(descripcion),
            registrado_por_usuario_id=registrado_por_usuario_id,
            creado_ts=ahora,
        )
        db.session.add(fila)
        db.session.flush()
        if commit:
            db.session.commit()
        return fila
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba.
        # Con `commit=False` la transacción es del llamador y el rollback
        # también.
        if commit:
            db.session.rollback()
        raise


def registrar_tanqueo(
    *,
    vehiculo_id: int,
    fecha: date,
    valor,
    galones,
    tanque: str,
    estacion: str,
    km: int,
    proveedor: str,
    origen_costo: str,
    registrado_por_usuario_id: int,
    documento_numero: Optional[str] = None,
    centro_op: Optional[str] = None,
    descripcion: Optional[str] = None,
    ts: Optional[datetime] = None,
) -> Tanqueo:
    """Registra un tanqueo: la fila de `flota_gasto` y su extremidad, juntas.

    QUÉ AFIRMA: que entraron esos galones a ese vehículo, en esa estación, con
    ese kilometraje, y que el tanque quedó como dice `tanque`.

    QUÉ NO AFIRMA:

    · **Que el rendimiento sea calculable.** Solo lo es de lleno a lleno. Un
      `parcial` o un `sin_dato` en un extremo no produce una ventana peor: no
      produce ninguna.
    · **Nada sobre nadie.** Si los galones exceden la capacidad de la ficha, lo
      que queda afirmado es que **dos datos no pueden ser los dos ciertos**. Una
      capacidad mal levantada, un tanque auxiliar que la ficha no conoce, dos
      vehículos tanqueados en la misma factura y un dedo en el teclado producen
      exactamente el mismo aviso, y ninguna se investiga mejor si el sistema ya
      dictó sentencia (regla 2).

    `tanque` **no tiene default y las tres opciones se eligen a mano**: un
    `lleno` puesto por inercia no produce un error, produce una ventana basura
    que infla o hunde la métrica sin que nada se vea raro (regla 1 del módulo).

    Las dos filas van en **una transacción**. Un gasto de combustible sin su
    tanqueo sería un peso que entra al CPK y no aporta un solo galón al
    rendimiento — visible en ninguna pantalla y equivocado en las dos métricas.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    if tanque not in costos.ESTADOS_TANQUE:
        raise GastoInvalido(
            f'estado de tanque desconocido: {tanque!r}. Conocidos: '
            f'{", ".join(costos.ESTADOS_TANQUE)}. Ninguno viene marcado: el '
            f'rendimiento solo existe de tanque lleno a tanque lleno.')
    if not (estacion or '').strip():
        raise GastoInvalido(
            'sin estación no se puede contestar dónde cobran más caro el galón, '
            'que es media razón por la que esta tabla existe.')

    cuantos = _decimal(galones, 'galones')
    if cuantos <= 0:
        raise GastoInvalido(
            f'{cuantos} galones no es un tanqueo gratis: es una fila mal '
            f'cargada, y dividir por ella produce un rendimiento infinito.')

    try:
        gasto = registrar_gasto(
            vehiculo_id=vehiculo_id, categoria='combustible', fecha=fecha,
            valor=valor, proveedor=proveedor, origen_costo=origen_costo,
            registrado_por_usuario_id=registrado_por_usuario_id, km=km,
            documento_numero=documento_numero, centro_op=centro_op,
            descripcion=descripcion, ts=ahora, commit=False)

        fila = Tanqueo(gasto_id=gasto.id, galones=cuantos,
                       tanque=tanque, estacion=estacion.strip())
        db.session.add(fila)
        db.session.commit()
        return fila
    except Exception:
        db.session.rollback()
        raise


def numero_legible(valor, decimales: int = 2) -> str:
    """Un número del dominio, como cadena que una persona pueda leer.
    `SIN_DATO` pasa entero.

    **Existe porque `str(Decimal)` no sirve para una pantalla**: `168000 ÷ 12`
    devuelve `Decimal('1.400E+4')`, y el renglón diría *«el galón costó
    1.400E+4»*. No es cosmético — un número que se lee mal se reporta mal, y
    este módulo ya publicó un `Carlos Pérez · undefined`.

    Vive acá y no en cada frontera porque tiene **dos consumidores**: el
    endpoint de gastos y el health. Escrito dos veces, el día que alguien
    cambie los decimales el tablero y el expediente van a mostrar números
    distintos del mismo mes, y no hay forma de saber cuál creer (regla 0).

    Redondea a centavos **solo para mostrar**. La aritmética no se toca: el
    canon fija tolerancia cero y esto formatea un renglón, no calcula un CPK.
    """
    if isinstance(valor, str):          # SIN_DATO y cualquier otra palabra
        return str(valor)
    paso = Decimal(1).scaleb(-decimales)
    return str(Decimal(valor).quantize(paso, rounding=ROUND_HALF_UP))


def _texto_o_none(valor: Optional[str]) -> Optional[str]:
    """Texto limpio, o `None`. **Nunca cadena vacía.**

    Dos representaciones de la misma ausencia son dos consultas que dan números
    distintos, y una de las dos es la que cuenta los gastos sin documento. El
    CHECK de la base también lo impide; esto es para que el error no llegue
    hasta allá con forma de 500.
    """
    limpio = (valor or '').strip()
    return limpio or None


# ── Lectura: el CPK, escrito una sola vez ────────────────────────────────────

def capacidad_de_tanque(vehiculo_id: int) -> Optional[Decimal]:
    """Los galones que caben, según la ficha. `None` si nadie la levantó.

    `None` y no un número por defecto: con una capacidad inventada el detector
    compararía galones reales contra un número que nadie midió, dispararía sobre
    operación sana y se apagaría en una semana.
    """
    ficha = FichaTecnica.query.filter_by(vehiculo_id=vehiculo_id).first()
    if ficha is None:
        return None
    return ficha.capacidad_tanque_galones


def excede_capacidad_de(tanqueo: Tanqueo) -> Union[bool, str]:
    """¿Este tanqueo metió más galones de los que caben? El juicio es del dominio.

    Una sola política: `costos.excede_capacidad`. Escribir acá un `>` sería la
    segunda copia, y la copia de la frontera es la que diverge cuando alguien
    decida que hace falta un margen del 5%.
    """
    return costos.excede_capacidad(
        galones=tanqueo.galones,
        capacidad_galones=capacidad_de_tanque(tanqueo.gasto.vehiculo_id))


def tanqueos_de(vehiculo_id: int) -> List[dict]:
    """Los tanqueos del vehículo en el formato que el dominio consume.

    Ordenados por kilometraje y no por fecha: las ventanas lleno-a-lleno se
    arman sobre el odómetro, y una factura cargada tarde con la fecha del día en
    que se digitó desordenaría la serie por el lado que importa. El empate se
    rompe por `id` para que la lista sea estable.
    """
    filas = (db.session.query(Tanqueo, Gasto, LecturaOdometro)
             .join(Gasto, Tanqueo.gasto_id == Gasto.id)
             .join(LecturaOdometro, Gasto.lectura_id == LecturaOdometro.id)
             .filter(Gasto.vehiculo_id == vehiculo_id).all())
    ordenadas = sorted(filas, key=lambda f: (f[2].valor_km, f[0].gasto_id))
    return [{'km': lec.valor_km, 'galones': Decimal(tq.galones),
             'tanque': tq.tanque, 'gasto_id': tq.gasto_id,
             'estacion': tq.estacion, 'fecha': g.fecha}
            for tq, g, lec in ordenadas]


def _tramo_de(lecturas: List[LecturaOdometro]):
    """Los kilómetros de la ventana y **con qué confianza se pueden publicar**.

    Devuelve `(km, marca)`. La marca la decide
    `flota/dominio/odometro.py::confianza_del_tramo` y **no se reimplementa acá**
    (regla 0): una segunda copia de esa política, escrita dentro de la consulta
    del tablero, sería la que diverge — y sería la que decide si un número se
    publica.

    Los dos extremos son la lectura de MENOR y la de MAYOR kilometraje de la
    ventana, que son exactamente las dos que producen la resta. Juzgar otras dos
    diría la confianza de un tramo que nadie calculó.

    Con menos de dos lecturas **no hay tramo**, y eso no es un tramo declarado:
    es `SIN_DATO`. Devolver `'declarada'` ahí afirmaría que la resta se pudo
    juzgar cuando no hubo resta. `costo_por_kilometro` traduce esa marca al
    `sin_dato` del canon.
    """
    if len(lecturas) < 2:
        return 0, SIN_DATO

    # Desempate por `(ts, id)`: hay lecturas del mismo segundo y con el mismo
    # kilometraje —`lecturas_ts_duplicado` cuenta diez—, y sin desempate la
    # misma ventana podría elegir una fila distinta en cada consulta y publicar
    # dos marcas distintas para el mismo mes.
    bajo = min(lecturas, key=lambda l: (l.valor_km, l.ts, l.id))
    alto = max(lecturas, key=lambda l: (l.valor_km, l.ts, l.id))
    marca = dom_odo.confianza_del_tramo(bajo.a_dominio(), alto.a_dominio())
    # `.value` y no `str(...)`: `str()` sobre un enum de Python 3.11 devuelve
    # `'Confianza.DUDOSA'`, y eso llegaría al tablero tal cual. SIN_DATO ya es
    # una cadena y pasa entero.
    return alto.valor_km - bajo.valor_km, (
        marca.value if isinstance(marca, Confianza) else marca)


def cpk_de(vehiculo_id: int, desde: date, hasta: date) -> dict:
    """El costo por kilómetro de UN vehículo en UNA ventana. Canon §5.

    Devuelve `{'cpk', 'marca', 'pesos', 'km', 'hubo_gastos'}` — el número **con
    todo lo que hace falta para desconfiar de él**. Un CPK suelto no se puede
    auditar: `pesos` y `km` son los dos insumos y salen con él.

    QUÉ AFIRMA: lo que dice el canon. Los pesos registrados contra este vehículo,
    con el gasto de período repartido, divididos entre los kilómetros que su
    odómetro dice que recorrió en la ventana.

    QUÉ NO AFIRMA: nada de las seis del canon §3 — no compara vehículos, no mide
    a nadie, no es el costo total de poseer el vehículo, no es contabilidad, no
    afirma que el gasto de período se haya consumido día a día y no explica una
    subida.

    Los kilómetros salen de la **primera y la última lectura dentro de la
    ventana**, no del odómetro actual: un vehículo cuya única lectura de marzo es
    del día 3 no recorrió los kilómetros de abril.

    `hubo_gastos` mira **todo el vehículo**, no la ventana. Es la distinción de
    los dos ceros del canon: sin ningún gasto registrado el CPK es `sin_dato`
    («nadie registró nada»); con gastos registrados y ninguno dentro de la
    ventana el CPK es **cero, y es un cero medido**.
    """
    gastos = Gasto.query.filter_by(vehiculo_id=vehiculo_id).all()

    pesos = sum(
        (costos.imputar_a_ventana(
            valor=Decimal(g.valor),
            periodo_desde=g.periodo_desde, periodo_hasta=g.periodo_hasta,
            ventana_desde=desde, ventana_hasta=hasta)
         for g in gastos),
        Decimal('0'))

    # ── La ventana se compara en DÍA OPERATIVO, no en UTC ────────────────
    #
    # `l.ts` es UTC naive; `desde`/`hasta` vienen de `dia_operativo()`, que es
    # Bogotá. Comparar `l.ts.date()` contra esa ventana corre el reloj cinco
    # horas: **todo turno cerrado entre las 7 p.m. y medianoche se contaba en el
    # día siguiente**, y en la frontera del mes se fugaba al mes siguiente.
    #
    # Medido el 2026-09-02 con tres turnos de agosto: el CPK salía exactamente
    # al DOBLE ($200/km contra $100/km reales) y lo hacía con la marca
    # `verificada` — la única que afirma que una persona miró. La confianza mide
    # la LECTURA; nadie medía la VENTANA.
    #
    # Los 1455 tests no lo veían porque todos sus `ts` son a las 08:00 UTC, la
    # única franja del día en que la fecha UTC y la de Bogotá coinciden. Es la
    # regla 5 del WMS —una fecha que alguien LEE como día va en Bogotá— violada
    # en código escrito esta misma semana.
    lecturas = [l for l in LecturaOdometro.query
                .filter_by(vehiculo_id=vehiculo_id).all()
                if desde <= _dia_operativo_de(l.ts) <= hasta]
    # Una sola lectura no delimita un tramo, y cero lecturas tampoco. **No es 0
    # km recorridos**: es que no se puede medir el tramo, y el dominio traduce
    # eso a `sin_dato` en vez de a un CPK con denominador inventado.
    km, marca_tramo = _tramo_de(lecturas)

    valor, marca = costos.costo_por_kilometro(
        pesos_imputados=pesos, km_recorridos=km,
        hubo_gastos=bool(gastos), marca_tramo=marca_tramo)

    return {'cpk': valor, 'marca': marca, 'pesos': pesos, 'km': km,
            'hubo_gastos': bool(gastos)}


def rendimiento_de(vehiculo_id: int) -> Union[Decimal, str]:
    """Rendimiento del vehículo sobre sus ventanas lleno-a-lleno.

    QUÉ NO AFIRMA: no mide a quien maneja y no se compara entre vehículos. La
    firma no recibe conductor y no debe recibirlo nunca.
    """
    return costos.rendimiento_km_galon(tanqueos_de(vehiculo_id))


__all__ = ['registrar_gasto', 'registrar_tanqueo', 'cpk_de', 'rendimiento_de',
           'numero_legible',
           'tanqueos_de', 'capacidad_de_tanque', 'excede_capacidad_de',
           'es_de_campo', 'ORIGEN_DE_LECTURA', 'GastoInvalido', 'SIN_DATO']
