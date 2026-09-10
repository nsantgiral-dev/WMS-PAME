# `control_flota` — el dueño del registro de la flota

> **En una frase:** lleva el expediente de cada vehículo, señala lo que está
> vencido y escala. **No aprueba nada, y eso está impuesto en el código.**

---

## Configuración

| Campo | Valor |
|---|---|
| `rol` | `control_flota` |

---

## Qué ve al entrar

Entra al panel de administración pero **solo ve la pestaña Flota**. Las demás
están ocultas, y no por estética: dejarle a la vista pestañas que el sistema le
va a negar con 403 enseña a ignorar los errores.

---

## Lo que SÍ podés

| Facultad | Nota |
|---|---|
| Cargar y editar **fichas técnicas** | Es el levantamiento de campo, tu trabajo principal |
| Registrar **documentos** (SOAT, RTM, póliza, tarjeta) | |
| Ver todos los turnos, custodias y fotos | |
| Ver el tablero: hallazgos, cierres forzados, **vehículos fuera de sede** | |
| Registrar lecturas de odómetro, y verificar las dudosas | |
| **Registrar** un gasto y la factura de una reparación | Sos el dueño del registro: consignar lo que ya pasó es tuyo |
| **Registrar** qué se hizo en una visita al taller | La orden la abre gestión; lo que se hizo adentro lo registrás vos |
| Reportar un daño | Reportar es de todos. El desenlace no |

---

## Lo que NO podés, y por qué está así

**No estás en el grupo `GESTION`.** Es deliberado y está escrito en el código:

> El procedimiento FLO-PR-01 dice que ves el tablero y escalás, pero **no
> aprobás órdenes de trabajo ni gastos**. Meterte en `GESTION` te daría permiso
> sobre liquidación, traslados y configuración de Siesa — y el documento diría
> una cosa mientras el sistema permite otra. Ese desfase es lo que vuelve
> decorativo un procedimiento.

### La frontera exacta: registrar sí, decidir no

| No podés | Quién lo hace |
|---|---|
| Abrir una orden de trabajo (mandar el camión al taller) | Gestión |
| Cerrar o anular una orden de trabajo | Gestión |
| Cerrar, descartar o aplazar un daño | Gestión |

Los tres comprometen plata o cierran un ciclo. Los tres se piden **escalando**,
que es literalmente lo que tu ficha dice que hacés: *no ordenás, señalás plazos
vencidos y escalás*. En la pantalla no vas a ver esos botones — no por adorno:
dejarte a la vista un gesto que el sistema te va a negar con 403 **enseña a
ignorar los errores**, que es la misma razón por la que se te esconden las otras
pestañas.

> **Por qué el desenlace del daño tampoco es tuyo, aunque sea registro.**
> «Días de hallazgo abierto» y «hallazgos vencidos» son **dos de las cinco
> señales con las que se mide si estás haciendo bien este trabajo** (más abajo).
> La regla 11 del módulo pregunta cómo maximiza una métrica quien no quiere
> hacer el trabajo; con el botón «Reparado» en la mano, la respuesta es de un
> clic y el registro queda impecable. **Quien es medido por un contador no puede
> tener el botón que lo baja.** No es desconfianza en vos: es que un indicador
> que su propio responsable puede cerrar no mide nada, y el que pierde es el que
> lo persigue.

**Tu autoridad no viene de aprobar. Viene de que el sistema calcula los plazos
por regla** y vos los hacés visibles. Un hallazgo bloqueante vence el mismo día,
uno mayor a 7 días, uno menor a 30 — no lo elegís vos ni lo negocia nadie.

---

## El trabajo principal: la ficha técnica

Es lo que ancla todo el módulo, y **solo se hace una vez en la vida del
vehículo**, parado al lado del camión:

| Dato | Por qué importa |
|---|---|
| **`km_inicial` + hora** | El ancla. Todo kilometraje posterior se valida contra este número |
| **posiciones de llanta** | Define cuántas fotos pide el recibo de turno |
| medida de llanta | |
| **distribución + de dónde salió** | Correa o cadena. **La base rechaza el dato sin procedencia** |
| **sistema de frenos + de dónde salió** | Idem — los dos disparan tareas de seguridad |
| combustible, aceites, norma de emisiones | |

> **Por qué la procedencia es obligatoria:** un dato conocido sin fuente es
> tradición oral con formato de columna. Si se sabe que la distribución es por
> correa, se sabe quién lo dijo — manual del fabricante, concesionario, taller o
> estimado. De ese dato depende una tarea de seguridad.

**El conductor no puede tocar la ficha.** `km_inicial` es el número que después
lo respalda a él; darle la llave sería darle el dato que lo evalúa.

---

## El tablero, y qué mirar

| Bloque | Qué significa |
|---|---|
| **Fuera de sede ahora** | Vehículos pasando la noche fuera del control de la empresa, con nombre de quién responde y motivo escrito |
| **Turnos cerrados a la fuerza** | Alguien cerró el turno de otro **sin fotos de cierre**. Si crece, no se está cerrando turno |
| Hallazgos vencidos | Con días de vencimiento |
| Documentos por vencer | 30 días de anticipación |

**Si un vehículo aparece "fuera de sede" tres semanas seguidas, dejó de ser una
excepción y es una costumbre que nadie decidió.** Verla es el primer paso para
decidirla.

---

## Cuando algo falla

### Un turno se cerró a la fuerza
Alguien cerró el turno de otro sin sus fotos. El turno siguiente arrancó sin nada
con qué comparar: si aparece un golpe, no se le puede atribuir a nadie.
**Llamá a los dos.** El procedimiento pide que quien fuerza avise el mismo día.

### Un vehículo sin ficha técnica
El recibo de turno funciona igual, pero pide las llantas **deducidas del tipo** y
lo declara en pantalla. Cargá la ficha: el número deja de ser un supuesto.

### Una foto quedó como `pendiente_evidencia`
La fila dice que hay foto y el archivo no está. Casi siempre es que el volumen no
está montado. **No es un dato que se pueda recuperar** — la foto se perdió.

### Un documento vencido
Es un hallazgo bloqueante: mismo día. No es negociable con el sistema.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Qué dice | Dónde |
|---|---|---|
| Fichas técnicas completas `health:fichas_completas` | Debería llegar a 100%. Una ficha creada con todo en `sin_dato` es una fila, no un dato | Flota → Analítica → *Lo que la ficha no dice* |
| Custodias sin las fotos completas `health:custodias_sin_foto_completa` | Sin fotos comparables, un golpe nuevo no se le puede atribuir a nadie — ni al conductor ni al turno anterior | Flota → Analítica → *Custodia* |
| **Cierres forzados por semana** `health:custodias_cerradas_forzadas` | Si crece, el problema no es el sistema: es que nadie está cerrando turno | Flota → Analítica → *Custodia* |
| Documentos vencidos `health:documentos_vencidos` | El vehículo no debería salir. Aparte van los que vencen en 30 días y los que nadie cargó — se corrigen distinto | Flota → Analítica → *Papeles* |
| Días de hallazgo abierto `health:dias_hallazgo_abierto` | Mide **riesgo real, no gestión**: el reloj para cuando el vehículo vuelve reparado, no al aprobar la orden. **No se compara entre zonas** — los tiempos de repuesto de Neiva, Pitalito y Florencia son distintos, y un promedio comparado mediría geografía | Flota → Analítica → *Días de hallazgo abierto* |

> Los cinco salen **despromediados**: primero la lista de casos con placa,
> después el agregado con su `n`. Con seis vehículos, un promedio no dice a qué
> camión llamar — y es lo único que hace falta saber para hacer algo.

### Cada cuánto

**Una vez por semana, unos 30 minutos.** El tablero está armado para eso: los
paneles que necesitan trabajo van arriba, y el primero —la calidad del
kilómetro— es el único cuyo tablero es a la vez el trabajo. Mientras esté en
rojo, el costo por kilómetro y la vida de llanta salen sin dato por diseño.

---

## Primera semana

| Día | Qué hace |
|---|---|
| 1 | Recorre los cinco vehículos con alguien de operación |
| 2 | **Carga dos fichas técnicas completas**, con procedencia |
| 3 | Observa un recibo y una entrega de turno reales |
| 4 | Carga las tres fichas restantes |
| 5 | Revisa el tablero completo y explica qué significa cada bloque |

---

## Cómo crecer

Este rol crece hacia **jefatura de flota**, cuando existan órdenes de trabajo y
tarifario de taller (tanda 3). Ahí sí aprueba gasto.

Qué demostrar antes: cinco fichas completas con procedencia, y **cero cierres
forzados durante un mes** — que significa que la operación adoptó el
procedimiento, no que el sistema lo obligó.

---
*Última revisión: 2026-09-09 · Verificado contra `flota/api/_permisos.py` y
contra la matriz rol × endpoint, que la ejerce por HTTP
(`tests/flota/test_matriz_roles_endpoints.py`).*

> La revisión del 2026-08-04 decía «verificado» y lo estaba: `MAESTROS_FLOTA`
> gateaba entonces dos endpoints —ficha y documentos—. Taller (2026-09-01) y
> gastos (2026-09-02) colgaron de la misma tupla sin que nadie se preguntara por
> este borde, y durante ocho días este documento afirmó que el código imponía
> algo que el código permitía. **Verificar contra el nombre de una tupla no es
> verificar: hay que verificar contra lo que la tupla gatea hoy.**
