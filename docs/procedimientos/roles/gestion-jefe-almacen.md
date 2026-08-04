# `jefe_almacen` — el dueño del inventario físico

> **En una frase:** responde porque lo que dice el sistema sea lo que hay en el
> estante. **Es el único que puede ajustar inventario directamente.**

---

## Configuración

| Campo | Valor |
|---|---|
| `rol` | `jefe_almacen` |
| `almacen_id` | **obligatorio** |

---

## Lo que SOLO vos y `admin` pueden hacer

Grupo `ALMACEN` = admin + jefe_almacen. Nadie más:

| Facultad | Por qué está acá |
|---|---|
| **Ajuste directo de inventario** | Cambia el número sin conteo. Es la facultad más peligrosa del sistema |
| **Maestros de almacén** — ubicaciones, layout | Un layout mal cargado desordena todos los recorridos |
| **Ver cédulas y teléfonos** de conductores | Dato personal: se restringe a quien lo necesita para operar |

> **El ajuste directo es la facultad que más se abusa.** Existe para corregir lo
> que el conteo ya demostró, no para cuadrar el sistema cuando molesta. Cada
> ajuste lleva motivo escrito y queda con tu nombre — porque el día que el
> inventario no cuadre, la pregunta va a ser quién lo ajustó y por qué.

---

## Lo que NO podés hacer

**No podés aprobar ajustes de conteo cíclico.** Eso es `LEAD` = admin +
supervisor. La separación es deliberada: quien maneja el inventario del día a día
no aprueba las correcciones del control que lo audita.

**No podés crear usuarios ni tocar la configuración de Siesa.** Eso es `admin`.

---

## El día

```
Mañana   · revisar descuadres reportados anoche
         · revisar la cola DLQ — jobs fallidos hacia Siesa
         · asignar capacidades (puede_picar / empacar / abastecer) del turno
Durante  · resolver bloqueos de piso
         · aprobar traslados (grupo DESPACHO)
Cierre   · revisar tareas colgadas
```

---

## Cuando algo falla

### Un producto no aparece en ninguna ubicación
No lo ajustes de una. **Mandalo a conteo.** Un ajuste sin conteo cambia el número
sin averiguar qué pasó, y el mismo faltante vuelve el mes que viene.

### La cola DLQ tiene jobs fallidos
Mirá el motivo antes de reintentar. Un job que falla tres veces por el mismo dato
malo va a fallar la cuarta.

### El layout no coincide con la realidad física
Corregilo en el sistema el mismo día. Un layout desactualizado hace caminar de
más a todos los operarios, todos los días, y nadie lo reporta porque se
acostumbran.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Qué dice |
|---|---|
| **Ajustes directos por mes** | **Si sube, algo aguas arriba está fallando.** Es el indicador que más importa |
| Descuadres detectados en conteo vs reportados por operarios | Si los operarios reportan poco, no confían en el proceso |
| Jobs DLQ fallidos sin resolver | |
| Tareas liberadas por zombi | Si sube, hay tareas que se abandonan |

**Un ajuste directo no es un logro: es la evidencia de que algo se rompió antes.**

---

## Primera semana

| Día | Qué hace |
|---|---|
| 1-2 | Recorre las tres operaciones de piso — picking, packing, recepción |
| 3 | Revisa la cola DLQ con alguien y entiende los tipos de job |
| 4 | Hace un ajuste completo con motivo, acompañado |
| 5 | Turno completo |

---

## Cómo crecer

**Siguiente: `admin`.** Qué demostrar: bajó los ajustes directos en 60 días, y
puede explicar la cadena completa de un pedido incluidos los conectores de Siesa.

---
*Última revisión: 2026-08-04*
