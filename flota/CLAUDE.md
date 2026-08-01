# FLOTA — Reglas del módulo

Módulo de control de flota dentro de WMS-PAME.
Este archivo contiene **solo reglas de decisión**. La referencia técnica vive en
`docs/flota/ESPECIFICACION_T1.md`. La historia vive en `docs/flota/ESTADO.md`.
Toda sección es una regla numerada y ninguna pasa de 15 líneas. Si una necesita más,
es un documento y va a `docs/flota/`: ahí es donde se sedimenta lo que no es decisión.

**Toda regla lleva su motivo.** Una regla sin motivo escrito tiene fecha de vencimiento:
alguien la "corrige" en seis meses por parecerle timorata.

---

## 1. Ningún ítem de inspección tiene valor por defecto

Un ítem no respondido es `sin_dato`, jamás `optimo`. Una inspección con `sin_dato` en un
ítem bloqueante no es `apto` ni `no_apto`: es `incompleta`, y no habilita despacho.

*Motivo: un default optimista convierte el sistema en una fábrica de evidencia falsa de
seguridad, y esa evidencia se usa después frente a un proceso disciplinario o una
aseguradora. `no_apto` es "sé que está mal". `incompleta` es "no sé". No saber tampoco
autoriza.*

## 2. Ningún automatismo imputa responsabilidad a una persona

El sistema propone un presunto responsable. Un humano decide. El afectado replica con
evidencia dentro de una ventana de 24 horas.

*Motivo: es la compuerta G4 de este dominio. Una sanción derivada de un cálculo automático
es un pasivo laboral y mata la adopción, que es el recurso más escaso del proyecto.*

## 3. Sin odómetro no se persiste ningún evento de flota

Inspección, custodia, orden de trabajo, tanqueo: todos llevan kilometraje.

*Motivo: el kilometraje es la única clave que une los tres formatos que hoy no se hablan
entre sí. Sin él no hay CPK, no hay preventivo por km y no se puede auditar si un
mantenimiento era necesario. Es exactamente el campo que le falta al SST-FO-12 en papel.*

## 4. Un estado que puede ser "no sé" se modela con palabras

Nunca con booleano ni con `None`.

*Motivo: un vehículo sin lecturas de odómetro no es un vehículo con 0 km recorridos. Una
tarea sin fecha de última ejecución no está al día ni vencida.*

## 5. Ningún adaptador degrada hacia algo que se parezca al éxito

Un `.get(x, default)` en una frontera es un bug: o funciona, o falla ruidosamente.
Prohibido heredar el `except Exception: pass` de `ruta_service.py:633`.

*Motivo: los defaults peligrosos de este dominio son "el último conductor conocido", "el
último odómetro conocido" y "el custodio del acta original" — este último es precisamente
lo que está pasando hoy en papel desde septiembre de 2025.*

## 6. Todo hallazgo nace con severidad y fecha límite

Bloqueante = mismo día. Mayor = 7 días. Menor = 30 días. Se calcula al nacer, no se elige
a mano. No existe transición `abierto → cerrado` directa: se cierra por OT, o se
`descarta` con motivo escrito.

*Motivo: los 30 días del THP 696. El sistema de papel detectaba bien; lo que no tenía era
reloj. Un hallazgo abierto sin fecha límite no es un control, es un pasivo.*

## 7. Toda foto nace atada a un ID de dominio, y hay dos clases de foto

Un archivo sin padre es un bug. **Evidencia de estado** (8 fotos de custodia, fotos de
hallazgo) usa los parámetros de compresión existentes. **Foto-dato** (tablero con
odómetro, factura, documento) va a mínimo 1600 px lado largo, calidad ≥75, y **no se
recomprime en servidor**. La base guarda referencia, hash, bytes y dimensiones — nunca el
binario.

*Motivo: el odómetro es un número de seis dígitos fotografiado a las 5 a.m. en patio. A
800×600 calidad 0.65 recomprimido a calidad 40 no es legible, y un odómetro que no se
puede verificar contra la foto es una declaración sin respaldo.*

## 8. Un doble de prueba se declara y deja rastro distinguible del real

`simulado = True` en el registro, no solo en el código.

*Motivo: `CanalNotificacionDev` costó una hora creyendo que 1.485 personas habían recibido
un cobro que nunca salió.*

## 9. Los POST contra SIESA no se reintentan nunca

Clave de idempotencia obligatoria. Un timeout no significa que falló.

*Motivo: una OT duplicada en el ERP es una factura duplicada.*

## 10. Todo cron que escribe nace apagado

Por variable de entorno. Antes de elegir el default, preguntar qué hace el primer ciclo,
no el estado estable.

*Motivo: el CRON de casos encendido contra QA.*

## 11. Antes de publicar una métrica: ¿cómo la maximiza quien no quiere hacer el trabajo?

Si es fácil de imaginar, la métrica está mal construida.

*Motivo: en este dominio la respuesta casi siempre es "marcando todo óptimo en veinte
segundos". Por eso se registra `segundos_llenado` y por eso los ítems no bloqueantes se
muestran en orden aleatorio.*

## 12. Ninguna feature nueva sin uso real de la anterior

Una tanda no empieza hasta que la anterior pasó su compuerta con un conductor real en
producción. Una cosa construida y sin verificar a la vez, no cinco.

*Motivo: es la lección más cara de cartera —superficie construida y nunca ejercitada— y
acá pesa más, porque el usuario puede negarse a usar el sistema sin que nadie se entere.*

## 13. Ningún número entra a ESTADO.md sin decir contra qué base y en qué fecha

*Motivo: `rutas_historicas_sin_placa` valía 0 en una SQLite vacía y se leyó como "todas
las rutas tienen placa". Era falso —son 3 de 15— y se leyó mal dos veces, la segunda
después de la advertencia. La defensa no es leer con más cuidado: es que el dato llegue
con su procedencia pegada.*

