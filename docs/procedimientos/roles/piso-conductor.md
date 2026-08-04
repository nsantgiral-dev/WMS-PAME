# `conductor` — entrega y custodia del vehículo

> **En una frase:** responde por un vehículo durante su turno y por la mercancía
> hasta que la firma el cliente. **Es el único rol que responde por un activo.**

---

## Configuración del usuario

| Campo | Valor | Nota |
|---|---|---|
| `rol` | `conductor` | |
| Ficha de conductor | **vinculada** | Ver abajo |

> **Importante:** un conductor tiene DOS registros — el `usuario` (para entrar a
> la app) y la ficha de `conductor` (con su cédula, que es lo que hace válida un
> acta). **Se vinculan, no se duplican.** Si creás una ficha nueva para alguien
> que ya tenía, sus rutas viejas quedan colgando de la ficha anterior.
>
> Se hace desde Rutas → Conductores → *crear cuenta* sobre la ficha existente.

---

## Dónde opera

**Sale del CO 003 (Bodega CD)** y entrega en la zona de la ruta. Su vehículo
puede quedar en cualquier sede, o fuera de sede — y eso se declara.

---

## Las dos responsabilidades, que son distintas

### 1 · La mercancía
Desde que carga hasta que el cliente firma. Recaudo incluido si es contado.

### 2 · El vehículo
**Desde que recibe el turno hasta que lo entrega.** Si aparece un golpe y no hay
fotos con las que comparar, no se le puede atribuir a nadie — ni a vos, ni al
que lo tuvo antes.

**Las fotos son tu protección, no un control sobre vos.** Un daño que ya estaba
al recibir y quedó fotografiado, es del turno anterior. Sin foto, es discutible.

---

## El día

```
5:00  RECIBIR TURNO
      · odómetro + foto del tablero
      · 13 fotos: 7 caras + una por llanta
      · quedás como responsable del vehículo

      RUTA
      · entregás, recaudás, tomás firma

18:00 ENTREGAR TURNO
      · odómetro + foto del tablero
      · 4 fotos: frontal, trasera, lateral izq, lateral der
      · declarás DÓNDE QUEDA el vehículo
```

**Por qué 13 al recibir y 4 al entregar:** recibir es exhaustivo porque te
protege a vos —estás asumiendo el vehículo—. Entregar es rápido porque cierra el
reloj y detecta un golpe nuevo. Si la entrega pidiera 13 fotos a las 6 p.m., a la
tercera semana serían 13 fotos del piso, y eso es peor que no tener nada: parece
registro y no lo es.

**El botón "cómo estaba"** te muestra la foto de cuando lo recibiste, para que
saques la de cierre desde el mismo ángulo. Sin el mismo encuadre, las dos fotos
no se pueden comparar y no prueban nada.

---

## Dónde queda el vehículo — los tres casos

| Opción | Quién responde después |
|---|---|
| **En la sede** (patio) | La sede |
| **En el taller** | La sede que lo envió |
| **Fuera de sede** | **Vos.** Sigue siendo tuyo |

**Fuera de sede exige motivo escrito** y aparece marcado en el tablero de control
de flota. No es un castigo: es un vehículo pasando la noche fuera del control de
la empresa, y eso tiene que verse.

---

## Lo que NO podés hacer, y por qué

**No podés recibir un vehículo que tiene otro conductor con el turno abierto.**
> En pantalla: *"El WHX245 lo tiene Víctor desde 02/08 a las 17:30. Si lo vas a
> recibir vos, Víctor tiene que cerrar su turno primero."*

Esa conversación la tienen ustedes dos. El sistema no la reemplaza.

**No podés cargar ni modificar la ficha técnica del vehículo.**
La ficha tiene el `km_inicial`, que es el número contra el que se valida todo tu
kilometraje después. Darte la llave de ese dato sería darte el dato que después
te respalda a vos. Lo carga control de flota.

**No podés registrar un odómetro menor al anterior.** La base lo rechaza. Si te
equivocaste al escribir, se corrige con una lectura nueva de tipo *corrección* y
motivo escrito — no se edita la anterior.

---

## Cuando algo falla

### No hay señal en el patio
La app guarda y sincroniza cuando vuelve. **Seguí.** No repitas el recibo —
se duplica el turno.

### El botón de confirmar tarda
Está subiendo las fotos. **No lo toques dos veces.** Te dice cuántas fotos y
cuántos KB está subiendo, y se deshabilita solo mientras trabaja.

### El vehículo tiene un daño que no estaba
Fotografialo **antes de salir** y avisá. Un daño reportado al recibir es del
turno anterior; el mismo daño reportado al volver es tuyo. La diferencia son dos
minutos.

### El cliente no está o rechaza el pedido
Se registra el rechazo con el motivo. Vuelve como devolución y liquidación genera
la nota crédito. **No lo dejes en la puerta.**

### Se cayó la app a mitad de ruta
Las entregas quedan en cola. Seguí entregando en papel y sincronizá al volver.

---

## Qué queda registrado de tu trabajo

El turno con hora de apertura y cierre, el kilometraje, las fotos, las entregas
y el recaudo.

**Es tu respaldo.** Si aparece un faltante de caja o un golpe en el camión, lo
que hay para responder es lo que registraste. El que registra bien tiene con qué;
el que no, tiene su palabra.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Dónde se ve |
|---|---|
| Turnos cerrados completos (con sus 4 fotos) | Tablero de control de flota |
| Turnos que te cerraron a la fuerza | Si aparecen, es que te fuiste sin cerrar |
| Rendimiento km/galón del vehículo | Tanqueo — *tanda 2* |
| Recaudo cuadrado | Liquidación |

---

## Primera semana

| Día | Qué hace | Se verifica con |
|---|---|---|
| 1 | Acompaña una ruta completa. **Se le explica que las fotos lo protegen a él.** | Puede explicar por qué 13 al recibir y 4 al entregar |
| 2 | Recibe y entrega turno acompañado | Las 13 fotos, cronometrado |
| 3 | **Fallos provocados**: sin señal, daño preexistente, cliente ausente | Resuelve 3 de 4 |
| 4 | Ruta corta solo | Turno cerrado completo |
| 5 | Ruta completa | Sin cierres forzados |

---

## Cómo crecer

**Siguiente paso: `control_flota`** — pasa de responder por un vehículo a llevar
el registro de todos.

Qué hay que demostrar:
- 60 días sin cierres forzados
- fotos utilizables (encuadre comparable, odómetro legible)
- reportó al menos un daño preexistente antes de salir — eso prueba que entendió
  para qué son las fotos

---
*Última revisión: 2026-08-04 · Flujo verificado contra `flota/api/` y `flota/dominio/`*
