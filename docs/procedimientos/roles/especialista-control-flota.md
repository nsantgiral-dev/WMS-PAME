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
| Registrar lecturas de odómetro | |

---

## Lo que NO podés, y por qué está así

**No estás en el grupo `GESTION`.** Es deliberado y está escrito en el código:

> El procedimiento FLO-PR-01 dice que ves el tablero y escalás, pero **no
> aprobás órdenes de trabajo ni gastos**. Meterte en `GESTION` te daría permiso
> sobre liquidación, traslados y configuración de Siesa — y el documento diría
> una cosa mientras el sistema permite otra. Ese desfase es lo que vuelve
> decorativo un procedimiento.

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

| Señal | Qué dice |
|---|---|
| Fichas técnicas cargadas / vehículos activos | Debería llegar a 100% |
| Turnos completos (13 fotos) / turnos abiertos | |
| **Cierres forzados por semana** | Si crece, el problema no es el sistema |
| Documentos vencidos sin gestionar | |
| Días promedio de hallazgo abierto | |

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
*Última revisión: 2026-08-04 · Verificado contra `flota/api/_permisos.py`*
