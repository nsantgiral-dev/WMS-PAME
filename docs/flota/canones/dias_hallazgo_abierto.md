# Canon — `dias_hallazgo_abierto`

> **Definido por Santiago el 2026-08-03.** Los tests salen de este documento, no
> del código. Si el cálculo y su prueba los produce la misma mano, la prueba es
> decoración.

---

## 1. Nombre

`dias_hallazgo_abierto`

## 2. Qué afirma, en palabras

**Días transcurridos entre el reporte de un hallazgo y su resolución física.**

## 3. Qué NO afirma

Cuatro cosas, y las cuatro importan porque el número va a disparar alertas y
eventualmente bloqueos:

- **No afirma que el vehículo estuvo detenido esos días.** Un hallazgo mayor
  abierto 20 días puede ser un camión rodando normal con un espejo rajado.
- **No afirma negligencia de nadie.** Un repuesto que tarda tres semanas en
  llegar produce el mismo número que un olvido.
- **No sirve para comparar zonas ni personas entre sí.** Los tiempos de taller,
  la disponibilidad de repuestos y la distancia son distintos en Neiva, Pitalito
  y Florencia. Un promedio comparado entre zonas mide geografía, no desempeño.
- **No mide el costo de la demora.** Eso es otro número y necesita el valor de
  la reparación y el de la ruta perdida.

## 4. Decisiones que fijan el cálculo

| Decisión | Respuesta | Motivo |
|---|---|---|
| ¿Cuándo arranca el reloj? | En el **timestamp del reporte** que registra el sistema | El reporte es digital y fechado: no hay ambigüedad de "qué día fue". Es justo la ambigüedad que el sistema de papel tenía y que este elimina |
| ¿Cuándo para? | Cuando **el vehículo vuelve reparado** y el hallazgo se marca cerrado con odómetro y evidencia | **Mide riesgo real, no gestión administrativa.** Si cerrara al aprobar la OT, un vehículo tres semanas en taller mostraría el indicador limpio |
| ¿Al aprobar la OT? | **No** | Aprobar es una firma, no una reparación |
| ¿Al entrar al taller? | **No** | Entrar al taller es logística, no resolución |
| ¿Calendario o hábiles? | **Calendario** | Un vehículo con una falla el domingo sigue con la falla el domingo |
| ¿El aplazamiento congela el reloj? | **No.** Aplazar cambia la fecha límite, no borra el tiempo transcurrido | Si congelara, aplazar sería la forma fácil de limpiar el tablero — y la métrica pasaría a medir cuánto se aplaza |

## 5. Caso calculado a mano — THP 696

**Insumo:** hallazgo reportado en la inspección del **6 al 11 de octubre**,
cerrado con las órdenes del **12 y 13 de noviembre**.

**Aritmética, en días calendario:**

| Reporte | Cierre | Días |
|---|---|---|
| 6 oct | 12 nov | 37 |
| 6 oct | 13 nov | 38 |
| 11 oct | 12 nov | 32 |
| 11 oct | 13 nov | 33 |

**Resultado: entre 32 y 38 días.** ⬜ PENDIENTE de fijar el par exacto.

> **Por qué este caso no se puede cerrar con la regla del punto 4, y eso es
> información y no un problema:** la regla dice que el reloj arranca *"en el
> timestamp del reporte que registra el sistema"* y que *"no hay ambigüedad
> porque el reporte es digital y fechado"*. **THP 696 es un caso de papel.** No
> tiene timestamp: tiene un rango de inspección de seis días y dos órdenes de
> cierre.
>
> Esa imposibilidad es exactamente el problema que el sistema resuelve. El caso
> sirve como referencia de magnitud —**el orden es un mes, no una semana**— y esa
> magnitud ya está fijada. El valor puntual necesita el par exacto de fechas.

## 6. Casos degenerados

Ninguno vale cero por omisión. Si la respuesta es "no sabemos", la respuesta es
`sin_dato`; si es "no corresponde", el hallazgo sale del indicador.

| Caso | Valor | Motivo |
|---|---|---|
| **Abierto, sin cerrar** | `sin_dato`, con el reloj corriendo | **Nunca cero ni un número grande.** Cero diría "se resolvió al instante"; un número grande lo mezclaría con los cerrados lentos. Está abierto: el dato que existe es cuántos días LLEVA, no cuántos duró |
| **Descartado con motivo** | Fuera del indicador | Se cuenta aparte. Un hallazgo descartado no se "resolvió": se determinó que no era un hallazgo |
| **Vehículo dado de baja antes de reparar** | Cierre como `no_aplica`, fuera del indicador | No hay resolución física posible. Contarlo como cerrado inventaría una reparación; dejarlo abierto lo dejaría corriendo para siempre |
| **Hallazgo de la inspección de línea base** | **Excluido, sin reloj** | La primera inspección de cada vehículo levanta lo que ya había. Nace preexistente, sin responsable y sin reloj — **el desorden viejo no entra al indicador.** La cuenta empieza al día siguiente, con todo fechado y con nombre |

## 7. Procedencia

- **Quién lo definió:** Santiago
- **Fecha:** 2026-08-03
- **Fuente del caso:** inspección en papel del THP 696, octubre–noviembre 2025
- **Implementación:** `flota/dominio/hallazgo.py`
- **Tests derivados:** `tests/flota/test_canon_hallazgo.py` — escritos desde este
  documento, antes del cálculo

## 8. Tolerancia

**Cero.** Son días enteros calendario, aritmética exacta sobre dos timestamps.
No hay punto flotante que redondear ni margen estadístico que admitir: si el
número no coincide, hay un bug o una decisión del punto 4 que cambió sin
actualizar este documento.
