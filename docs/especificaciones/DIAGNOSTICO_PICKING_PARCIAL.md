# Diagnóstico Técnico — Despacho Parcial: 142945 ignora f470_cant_base

> **Generado:** 2026-05-25  
> **Proyecto:** WMS-PAME — Papelería Medellín  
> **Ambiente activo:** QA → `integradorqa.siesacloud.com`  
> **Archivo de referencia código:** `app/services/connekta_gateway.py`, `app/services/despacho_parcial_service.py`

---

## 1. Contexto del problema

El WMS envía despachos parciales vía conector **142945** (`API_v1_Ventas_Comercial_RemisionPedido`).
El payload incluye:

```json
{
  "f470_cant_base": 3.0,
  "f470_rowid_movto": 470406
}
```

**Resultado esperado:** SIESA graba 3 unidades en T470.  
**Resultado real:** SIESA graba **4 unidades** (la cantidad comprometida completa).

---

## 2. Investigación realizada — lo que se descartó

### 2.1 Perfil de usuario Connekta (`m.agudelo`)
- Verificado en **Administrador de Seguridad SIESA**: usuario `m.agudelo` ✅ asignado al perfil **Administrador**.
- Checkbox **"No almacenar cantidades parciales al aprob y comp"** → ☑ **UNCHECKED** en perfil Administrador.
- **Conclusión:** La configuración de seguridad es correcta. Este no es el problema.

### 2.2 Código del WMS
- `f470_rowid_movto` se envía correctamente (valor real: `470406`, que es `f431_rowid` de T431).
- `f470_cant_base` se envía con el valor real picado (3.0).
- Commit `6028daa` verificó que `rowid_map` retorna valores reales.
- **Conclusión:** El código del WMS es correcto. No hay código que tocar.

### 2.3 Backorder del cliente
- Estado de backorder en proceso de estandarización con Siesa (2026-05-20).
- **Conclusión:** No es la causa del problema de cantidad. Son issues separados.

---

## 3. Causa raíz — diagnóstico definitivo

### Arquitectura interna de SIESA para despachos parciales

```
T431  t431_cm_pv_movto   (líneas del pedido)
  └─► T405  t405_cm_compromisos
              ├─ f405_cant_por_remisionar_base = 4.0  ← comprometido por SIESA
              └─ f405_cant_picking_base        = NULL  ← vacío (WMS no lo escribió)
```

**El conector 142945 tiene lógica interna que:**
1. Recibe `f470_rowid_movto` (= `f431_rowid`) para encontrar la fila en T431.
2. Desde T431 navega a T405.
3. Lee `f405_cant_picking_base` → si es NULL → usa `f405_cant_por_remisionar_base` = **4**.
4. **Ignora `f470_cant_base`** del payload cuando `f405_cant_picking_base` es NULL.

### El botón "Parciales" en la UI de SIESA

El flujo manual que hace un usuario en SIESA:
1. Abre pantalla **Remisiones desde Pedidos**.
2. Presiona botón **"Parciales"**.
3. SIESA escribe `f405_cant_picking_base = 3` en T405 para ese compromiso.
4. Crea la remisión → 142945 lee `f405_cant_picking_base` = 3 → graba 3 en T470.

**El WMS necesita replicar el paso 3 de forma programática antes de llamar 142945.**

---

## 4. Solución implementada en Connekta QA

### 4.1 Lo que NO se puede hacer — Conectores dinámicos

En `Apis Dinámicas Siesa → Conectores`:
- Solo se pueden **importar** conectores del catálogo oficial de SIESA.
- **No se pueden crear conectores nuevos** desde cero.
- No existe en el catálogo un conector que actualice `f405_cant_picking_base`.

### 4.2 Lo que SÍ se hizo — Consulta dinámica (UPDATE vía Módulo Conectividad)

En `Apis Dinámicas Siesa → Consultas`, se creó:

| Campo | Valor |
|-------|-------|
| **ID** | `7811` |
| **Nombre** | `papeleriamedellin_WMS_set_picking_parcial` |
| **Conexión** | Módulo conectividad (SQL Server local) |
| **Versión** | 3.0 |
| **Estado** | ✅ Activo |

**SQL configurado:**
```sql
UPDATE t405_cm_compromisos
SET    f405_cant_picking_base = @cantidad
WHERE  f405_id_cia = 1
  AND  f405_rowid_pv_movto = @f431_rowid
  AND  f405_cant_por_remisionar_base >= @cantidad
  AND  @cantidad > 0
```

**Guards de seguridad del SQL:**
- `f405_id_cia = 1` → solo empresa PAME, nunca cruza datos
- `f405_rowid_pv_movto = @f431_rowid` → fila exacta del compromiso, no masivo
- `f405_cant_por_remisionar_base >= @cantidad` → imposible despachar más de lo comprometido
- `@cantidad > 0` → nunca borra el picking con un 0 accidental

---

## 5. Bloqueo actual — Permiso pendiente de Siesa Soporte

### Por qué falla ahora

La consulta 7811 existe y está activa. Pero cuando el WMS la llame:

```
WMS → GET /api/connekta/v3/ejecutarconsulta (param: 7811)
  → Connekta Cloud
    → Módulo Conectividad Siesa SQL Server (agente local)
      → SQL Server: SUnoEE_Papeleriamed_Imple
        → UPDATE t405_cm_compromisos ← ❌ ERROR: permiso denegado
```

El **agente local** del Módulo de Conectividad se conecta a SQL Server con un usuario de servicio distinto a `m.agudelo`. Ese usuario **no tiene permiso UPDATE** sobre `dbo.t405_cm_compromisos`.

---

## 6. Mensaje para Siesa Soporte (listo para enviar)

> **Asunto:** Habilitar permiso UPDATE en t405_cm_compromisos para Módulo de Conectividad — QA y Producción
>
> Hola equipo,
>
> En el ambiente QA (`integradorqa.siesacloud.com`) creamos la consulta dinámica **ID 7811** — `papeleriamedellin_WMS_set_picking_parcial`, que ejecuta un `UPDATE` sobre la tabla `dbo.t405_cm_compromisos` (campo `f405_cant_picking_base`) a través del **Módulo de Conectividad Siesa SQL Server**.
>
> **Lo que necesitamos:**
> Que el usuario de base de datos que usa el Módulo de Conectividad para conectarse a `SUnoEE_Papeleriamed_Imple` tenga permiso de escritura sobre esa tabla:
>
> ```sql
> GRANT UPDATE ON dbo.t405_cm_compromisos
>     TO [<usuario_servicio_modulo_conectividad>];
> ```
>
> **Contexto técnico:**
> El WMS necesita escribir `f405_cant_picking_base` antes de ejecutar el conector 142945 (RemisionPedido), para que SIESA respete la cantidad real picada por el operario en lugar de la cantidad comprometida completa. Esto replica el comportamiento del botón "Parciales" de la pantalla de Remisiones desde Pedidos.
>
> **Preguntas adicionales para el ticket:**
> 1. ¿Cuál es el nombre del usuario de SQL Server que usa el Módulo de Conectividad para conectarse a la BD?
> 2. ¿Las consultas dinámicas de tipo `UPDATE`/`DML` son soportadas por el endpoint `ejecutarconsulta`, o se requiere un endpoint diferente?
> 3. ¿Este mismo permiso aplica también para el ambiente de **producción** (`servicios.siesacloud.com`) o se gestiona por separado?

---

## 7. Código WMS listo para integrar (pendiente permiso)

### 7.1 `connekta_gateway.py` — nuevo método

```python
def set_picking_parcial(self, f431_rowid: int, cantidad: float) -> dict:
    """
    Consulta dinámica ID 7811 — papeleriamedellin_WMS_set_picking_parcial
    
    PREREQUISITO OBLIGATORIO antes de llamar trigger_despacho() (142945).
    Escribe f405_cant_picking_base en T405 para que 142945 respete
    f470_cant_base en lugar de usar f405_cant_por_remisionar_base.
    
    Equivale al botón "Parciales" de la UI de Remisiones desde Pedidos.
    
    Args:
        f431_rowid: f431_rowid de T431 (línea del pedido) — viene de get_pedido_rowid_map()
        cantidad:   Unidades reales picadas por el operario en el WMS
    """
    if self.modo_simulacion:
        logger.info(
            '[CONNEKTA SIMULACION] set_picking_parcial: rowid=%s cant=%s',
            f431_rowid, cantidad
        )
        return {'simulado': True}

    logger.info(
        '[CONNEKTA] set_picking_parcial → f431_rowid=%s cantidad=%s',
        f431_rowid, cantidad
    )
    return self._get(
        'papeleriamedellin_WMS_set_picking_parcial',
        params_extra={
            'parametros': f'f431_rowid={f431_rowid}|cantidad={cantidad}'
        },
        url=self.url_get_dinamico,
    )
```

### 7.2 `despacho_parcial_service.py` — insertar antes del bloque de 142945

Buscar el bloque:
```python
# 3. Idempotencia para 142945 ...
```

Insertar ANTES:
```python
# Paso 0 — escribir cant_picking en T405 (prerequisito para 142945)
# Sin esto, 142945 usa f405_cant_por_remisionar_base (cantidad comprometida completa)
# en lugar de respetar el f470_cant_base que enviamos.
for ref, rowid in rowid_map.items():
    cant = float(cantidades.get(ref, 0))
    if cant > 0 and rowid:
        try:
            connekta.set_picking_parcial(f431_rowid=rowid, cantidad=cant)
            logger.info(
                '[DESPACHO_PARCIAL] T405 picking seteado: ref=%s rowid=%s cant=%s (tarea=%s)',
                ref, rowid, cant, tarea.id
            )
        except Exception as e:
            # No bloquear el despacho si el UPDATE falla — loguear y continuar.
            # 142945 usará cant_por_remisionar como fallback (comportamiento previo).
            logger.warning(
                '[DESPACHO_PARCIAL] set_picking_parcial falló para ref=%s: %s — '
                'continuando con 142945 (usará cantidad comprometida)',
                ref, e
            )
```

---

## 8. Inventario de consultas dinámicas activas en Connekta QA

| ID | Nombre | Uso |
|----|--------|-----|
| 6719 | `papeleriamedellin_WMS_Picking_Pedidos_NB1` | Pedidos en estado picking para bodega NB1 |
| 7461 | `papeleriamedellin_test_remisiones_temp` | Testing temporal — verificar si se puede eliminar |
| 7479 | `papeleriamedellin_WMS_Remision_DesdePedido` | Auto-detect consecutivo RM post-142945 |
| 7798 | `papeleriamedellin_compromisos_wms` | Mapeo `{f431_rowid: f405_rowid}` para pedidos |
| 7799 | `papeleriamedellin_pame_descubrir_tablas` | Introspección de tablas — uso diagnóstico |
| **7811** | **`papeleriamedellin_WMS_set_picking_parcial`** | **UPDATE T405 — NUEVO, pendiente permiso** |

---

## 9. Inventario de conectores dinámicos activos en Connekta QA

| ID | Nombre | Módulo | Sub-módulo |
|----|--------|--------|------------|
| 238920 | `CLASIFICACION DE ITEMS` | 01_Maestro | 3_Comercial |
| 238925 | `FACTURA_DESDE_PEDIDO` | 03_Comercial | 2_Ventas |
| 239216 | `API_v1_Ventas_Comercial_RemisionPedido` | 12_Connekta | 6_ConectoresEstandar |
| 244114 | `ACTUALIZACION_BACKORDER_PAME_V1` | 01_Maestro | 3_Comercial |

> ⚠️ En conectores solo se puede IMPORTAR del catálogo SIESA. No se pueden crear nuevos.

---

## 10. Checklist de próximos pasos

```
[✅] Consulta 7811 creada y activa en Connekta QA
[✅] SQL validado con guards de seguridad correctos
[✅] Código WMS preparado en connekta_gateway.py y despacho_parcial_service.py

[ ]  BLOQUEADO: Siesa Soporte habilita GRANT UPDATE en t405 para usuario Módulo Conectividad
[ ]  Confirmar con Soporte que ejecutarconsulta soporta DML (UPDATE), no solo SELECT
[ ]  Una vez habilitado: test manual con pedido real en QA
         → Verificar log: "[DESPACHO_PARCIAL] T405 picking seteado"
         → Verificar en SIESA: T470.f470_cant_base = cantidad_real (no cantidad_comprometida)
[ ]  Si test OK: commit + deploy Railway QA
[ ]  Replicar consulta en producción (servicios.siesacloud.com) con mismo SQL
[ ]  Replicar permiso GRANT en producción
[ ]  Test end-to-end producción con 1 pedido de prueba
```

---

## 11. Referencias de código

| Archivo | Líneas clave |
|---------|-------------|
| `app/services/connekta_gateway.py` | `get_pedido_rowid_map()` — genera el `rowid_map {ref: f431_rowid}` |
| `app/services/connekta_gateway.py` | `get_compromisos_pedido()` — obtiene `f431_rowid` desde `API_v2_Ventas_Pedidos_Compromisos` |
| `app/services/connekta_gateway.py` | `trigger_despacho()` — conector 142945, usa `f470_rowid_movto` |
| `app/services/despacho_parcial_service.py` | `despachar_parcial()` — orquestador principal |
| `app/services/despacho_parcial_service.py` | `_build_items()` — construye items con `rowid_movto` |

---

*WMS-PAME — Documento generado 2026-05-25 — Siguiente acción: ticket a Siesa Soporte*
