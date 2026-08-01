# PLANTILLA de canon — métrica de flota

Copiar a `docs/flota/canones/<metrica>.md` y llenar. **La llena quien NO va a
implementar la métrica.** Si el valor y su test los produce la misma mano, el
test es decoración: verifica que el código hace lo que el código hace.

Formato Markdown y no JSON como los canones de `docs/canones/` a propósito: los
de allá fijan la salida de una corrida certificada de un modelo estadístico.
Estos fijan **la definición de un número operativo antes de que exista código**,
y lo que hay que escribir son decisiones en palabras, no arreglos de floats.

Las cuatro reglas que lo hacen valer algo siguen siendo las mismas:

1. Los valores son la salida de un caso real trabajado a mano, nunca constantes
   de diseño ni números recordados de una conversación.
2. **Procedencia obligatoria.** Sin ella el canon es tradición oral con formato.
3. La tolerancia cubre redondeo, no permisividad. Si la prueba falla, se
   investiga la diferencia — jamás se afloja el criterio para que pase.
4. Los insumos del caso se identifican sin ambigüedad (documento, fechas,
   placa), para poder distinguir un bug de un cambio de datos.

---

## 1. Nombre

`<nombre_exacto_de_la_metrica>`

**El nombre debe ser derivable de la definición.** Si al leer la sección 2 el
nombre no se deduce, el nombre está mal — no la definición. Un identificador que
promete una cosa y calcula otra no falla ruidosamente: miente con confianza.

## 2. Qué afirma, en palabras

⬜ SIN DEFINIR

## 3. Qué NO afirma

⬜ SIN DEFINIR

Obligatorio. Es la sección que evita que alguien consuma el número para una
pregunta que no responde.

## 4. Decisiones que fijan el cálculo

Cada una se responde con una frase y su motivo. Sin motivo, la decisión tiene
fecha de vencimiento.

| Decisión | Respuesta | Motivo |
|---|---|---|
| ⬜ | ⬜ | ⬜ |

## 5. Caso calculado a mano

Un caso real, identificado, con el resultado calculado **fuera del sistema**.

- **Insumo:** ⬜ (documento / placa / fechas exactas)
- **Cálculo paso a paso:** ⬜
- **Resultado:** ⬜

## 6. Casos degenerados

Qué vale la métrica cuando el caso normal no aplica. Ninguno puede valer 0 por
omisión: si la respuesta es "no sabemos", la respuesta es `sin_dato`.

| Caso | Valor | Motivo |
|---|---|---|
| ⬜ | ⬜ | ⬜ |

## 7. Procedencia

- **Quién lo calculó:** ⬜
- **Fecha:** ⬜
- **Fuente de los datos:** ⬜ (planilla, acta en papel, foto, export)

## 8. Tolerancia

⬜ SIN DEFINIR — y qué cubre exactamente.
