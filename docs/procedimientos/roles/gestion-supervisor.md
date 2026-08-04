# `supervisor` — el control del control

> **En una frase:** aprueba lo que el piso no puede aprobar solo. **Es la segunda
> firma.**

---

## Configuración

| Campo | Valor |
|---|---|
| `rol` | `supervisor` |
| `almacen_id` | recomendado |

---

## Lo que SOLO vos y `admin` pueden hacer

Grupo `LEAD` = admin + supervisor:

**Aprobar ajustes de inventario del conteo cíclico.**

Esa es la facultad. Y el motivo de que exista es uno solo:

> Quien cuenta no aprueba. Si la misma persona hace las dos cosas, el conteo
> deja de significar algo — cualquier faltante se cierra solo.

**Ni el jefe de almacén está en este grupo**, y no es un olvido: el jefe maneja
el inventario del día a día, así que no puede aprobar las correcciones del
control que lo audita.

---

## También podés

- Liquidación de rutas (NC → RC → DC en Siesa)
- Aprobar traslados (grupo `DESPACHO`)
- Ver el tablero del Vigía
- Reintentar jobs de la cola DLQ

---

## Lo que NO podés hacer

**No podés hacer ajustes directos de inventario** (eso es `ALMACEN`). Vos aprobás
lo que el conteo demostró; no cambiás el número por tu cuenta.

**No podés crear usuarios ni tocar Siesa.** Eso es `admin`.

---

## El día

```
Mañana   · aprobar los ajustes de conteo de anoche
         · revisar el Vigía: alarmas abiertas
Tarde    · liquidar las rutas que volvieron
Cierre   · revisar cierres forzados de turno en flota
```

---

## Aprobar un ajuste: qué mirar antes de firmar

1. **¿Hubo segundo conteo?** Si CC1≠CC2 tiene que existir CC3. Si no, algo se
   saltó.
2. **¿El descuadre tiene un patrón?** Un producto que descuadra tres meses
   seguidos no es un error de conteo: es un problema de proceso.
3. **¿Contó alguien distinto cada vez?** Si es la misma persona en CC1 y CC2, el
   segundo conteo no vale.

**Firmar rápido es lo que vuelve decorativo el conteo cíclico.**

---

## Cuando algo falla

### Un descuadre enorme
Antes de aprobar, mandalo a un tercer conteo con otra persona. Un ajuste grande
mal aprobado se arrastra meses.

### Una alarma del Vigía
Se cierra con causa escrita (mínimo 20 caracteres) y un responsable del plan.
**Cerrar una alarma sin causa real es silenciar el detector** — y el módulo entero
existe para avisar de un desplome operativo antes de que sea evidente.

### La liquidación no cuadra
No fuerces el cierre. La diferencia entre lo despachado y lo recaudado es
información: puede ser devolución, rechazo, o faltante de caja. Los tres se
resuelven distinto.

---

## Cómo se sabe que lo estás haciendo bien

| Señal | Qué dice |
|---|---|
| Ajustes aprobados sin tercer conteo cuando correspondía | Debería ser cero |
| Tiempo entre descuadre y aprobación | Si crece, el piso deja de reportar |
| Alarmas del Vigía cerradas vs reabiertas | Si se reabren, la causa no era la real |
| Rutas liquidadas el mismo día | |

---

## Primera semana

| Día | Qué hace |
|---|---|
| 1 | Recorre el piso. Entiende de dónde sale un descuadre |
| 2 | Aprueba ajustes acompañado — **con las tres preguntas** |
| 3 | Liquida una ruta acompañado |
| 4 | Cierra una alarma del Vigía con causa real |
| 5 | Turno completo |

---

## Cómo crecer

**Siguiente: `admin`.**

Qué demostrar: 60 días sin ajustes aprobados que después haya que revertir, y
puede explicar por qué el jefe de almacén no está en `LEAD`.

---
*Última revisión: 2026-08-04*
