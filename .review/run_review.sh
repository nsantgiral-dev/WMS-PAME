#!/usr/bin/env bash
# ============================================================
# WMS-PAME Review Agents Orchestrator v2
# 9 agentes especializados en paralelo → reporte HTML ejecutivo
# ============================================================

set -euo pipefail

# ── Configuración ────────────────────────────────────────────
PROYECTO_DIR="${WMS_DIR:-$HOME/PROYECTOS/WMS-PAME-1}"
APP_DIR="$PROYECTO_DIR/app"
# El modulo de flota vive FUERA de app/ (frontera hexagonal: dominio puro,
# puertos, adaptadores, api). Sin esto ninguno de los 9 agentes lo veia.
FLOTA_DIR="$PROYECTO_DIR/flota"
REVIEW_DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPTS_DIR="$REVIEW_DIR/prompts"
REPORTS_DIR="$REVIEW_DIR/reports"
TEMP_DIR=$(mktemp -d)
FECHA=$(date +%Y-%m-%d)
HORA=$(date +%H:%M)
LOG_FILE="$REPORTS_DIR/${FECHA}_review.log"

# Modelo a usar (cambiar a claude-opus-4-6 para análisis más profundo)
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"
# Cuántos agentes a la vez. Nueve en paralelo con contextos de 200-380 KB se
# atropellan — medido el 2026-08-03: 2 murieron y 2 devolvieron basura. Con 3
# tarda mas y termina.
CONCURRENCIA="${CONCURRENCIA:-3}"

# Límite de chars por contexto de agente (200KB ≈ ~50K tokens)
# Presupuesto por agente: ~200 KB de input TOTAL (prompt + codigo + flota).
#
# No es un numero de gusto: el 2026-08-03, con nueve agentes en paralelo, los
# inputs de 374 y 381 KB fallaron, los de 212 y 223 KB murieron sin escribir, y
# los de 154 a 205 KB terminaron bien. Se elige el rango que funciono.
MAX_CHARS_SERVICES=100000
MAX_CHARS_ROUTES=140000
MAX_CHARS_SIESA=150000
MAX_CHARS_ALL=140000
# Flota entera mide ~162KB: entra completa. Se agrega COMO BLOQUE APARTE,
# despues del corte de app/, para que no le robe espacio a lo que ya se
# revisaba — app/services solo mide 1.2MB contra una cota de 120KB, asi que
# cualquier byte que flota ocupe ahi es un byte de servicios que se deja de ver.
MAX_CHARS_FLOTA=100000
# Para los contextos que ya van llenos de app/ (performance, tech_debt) flota
# entra con una porcion mas chica: el dominio, que es donde vive la politica.
MAX_CHARS_FLOTA_CORTO=55000

# ── Colores ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

log() { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $1" | tee -a "$LOG_FILE"; }
ok()  { echo -e "${GREEN}✓${NC} $1"  | tee -a "$LOG_FILE"; }
err() { echo -e "${RED}✗ ERROR:${NC} $1" | tee -a "$LOG_FILE"; }
warn(){ echo -e "${YELLOW}⚠${NC} $1" | tee -a "$LOG_FILE"; }

# ── Encabezado ───────────────────────────────────────────────
echo -e "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  🤖 WMS-PAME Review Agents v2 — ${FECHA} ${HORA}${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

mkdir -p "$REPORTS_DIR"

# ── Verificaciones ───────────────────────────────────────────
if ! command -v claude &>/dev/null; then
    err "Claude Code CLI no encontrado. Instala con: npm install -g @anthropic-ai/claude-code"
    exit 1
fi

if [ ! -d "$PROYECTO_DIR" ]; then
    err "Directorio del proyecto no encontrado: $PROYECTO_DIR"
    err "Ajusta la variable WMS_DIR o el path en el script"
    exit 1
fi

# ── Script de extracción JSON (evita problemas de escaping en bash) ──
EXTRACT_PY="$TEMP_DIR/extract_json.py"
cat > "$EXTRACT_PY" << 'PYEOF'
#!/usr/bin/env python3
"""Extrae el JSON de respuesta del agente de forma robusta."""
import sys
import json
import re

agent_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
content = sys.stdin.read().strip()

def try_parse(s):
    """Intenta parsear s como JSON con 'agent' e 'issues'."""
    try:
        obj = json.loads(s)
        if isinstance(obj, dict) and "agent" in obj and "issues" in obj:
            return obj
    except Exception:
        pass
    return None

# Intento 1: parseo directo
result = try_parse(content)

# Intento 2: primer '{' al último '}'
if not result:
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        result = try_parse(content[start : end + 1])

# Intento 3: bloques ```json ... ```
if not result:
    for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL):
        result = try_parse(block)
        if result:
            break

# Fallback
if not result:
    result = {
        "agent": agent_name,
        "issues": [],
        "summary": "No se pudo parsear la respuesta del agente.",
        "score": 0,
    }

print(json.dumps(result, ensure_ascii=False))
PYEOF

# ── Función: construir contexto de código por directorio/archivo ──
# Uso: build_context <archivo_destino> <max_chars> <dir_o_archivo> [dir_o_archivo ...]
build_context() {
    local ctx_file="$1"
    local max_chars="$2"
    shift 2

    > "$ctx_file"

    # Recolectar lista de archivos
    local all_files=()
    for target in "$@"; do
        if [ -d "$target" ]; then
            while IFS= read -r f; do
                all_files+=("$f")
            done < <(find "$target" -type f -name "*.py" \
                ! -path "*/__pycache__/*" \
                ! -path "*/venv/*" \
                ! -path "*/.venv/*" \
                ! -path "*/migrations/*" \
                2>/dev/null | sort)
        elif [ -f "$target" ]; then
            all_files+=("$target")
        fi
    done

    # Escribir archivos al contexto respetando el límite
    local leidos=0
    for archivo in "${all_files[@]}"; do
        [ -r "$archivo" ] || continue

        # Se PROYECTA el tamaño: antes se miraba el acumulado y recién ahí se
        # cortaba, así que el último archivo entraba entero por encima del
        # límite. El contexto de `performance` terminó en 350 KB contra una cota
        # de 180 KB, y ese exceso es lo que mató al agente.
        local current_size file_size
        current_size=$(wc -c < "$ctx_file" 2>/dev/null || echo 0)
        file_size=$(wc -c < "$archivo" 2>/dev/null || echo 0)
        # Si el archivo NO cabe entero, entra RECORTADO — no se salta.
        #
        # `connekta_gateway.py` mide 184 KB, mas que la cota de su propio
        # contexto. Saltarlo dejaba al agente de Siesa revisando la integracion
        # sin el archivo de la integracion. Medio archivo dice algo; ninguno no.
        local restante=$(( max_chars - current_size ))
        if [ "$file_size" -gt "$restante" ]; then
            if [ "$restante" -gt 20000 ]; then
                printf '=== ARCHIVO (RECORTADO a %d de %d bytes): %s ===\n' \
                    "$restante" "$file_size" "${archivo#$PROYECTO_DIR/}" >> "$ctx_file"
                head -c "$restante" "$archivo" >> "$ctx_file"
                printf '\n=== [CORTE — el resto del archivo no entra] ===\n' >> "$ctx_file"
                leidos=$(( leidos + 1 ))
            fi
            # Los que FALTAN, no el total: el mensaje anterior imprimia
            # ${#all_files[@]} —todos— y hacia parecer que no se habia leido
            # nada. Un aviso con el numero equivocado es peor que ninguno.
            printf '\n=== [TRUNCADO: límite de %d bytes alcanzado — %d de %d archivos omitidos] ===\n' \
                "$max_chars" "$(( ${#all_files[@]} - leidos ))" "${#all_files[@]}" >> "$ctx_file"
            break
        fi

        printf '=== ARCHIVO: %s ===\n' "${archivo#$PROYECTO_DIR/}" >> "$ctx_file"
        cat "$archivo" >> "$ctx_file"
        printf '\n' >> "$ctx_file"
        leidos=$(( leidos + 1 ))
    done
}

# ── Función: agrega el módulo flota como bloque propio ──
# Se llama DESPUES de build_context, con su propia cota: asi flota es aditiva y
# no compite por el presupuesto de app/, que ya se trunca al 10%.
append_flota() {
    local ctx_file="$1"
    shift
    [ -d "$FLOTA_DIR" ] || return 0
    printf '\n\n=== MODULO FLOTA (fuera de app/, frontera hexagonal) ===\n' >> "$ctx_file"
    local tmp="${ctx_file}.flota"
    build_context "$tmp" "$MAX_CHARS_FLOTA" "$@"
    cat "$tmp" >> "$ctx_file"
    rm -f "$tmp"
}

# ── Función: contexto especializado para agente siesa_logic ──
build_siesa_context() {
    local ctx_file="$1"
    local max_chars="$2"

    > "$ctx_file"

    # Archivos de servicios relacionados con Siesa/integración
    local siesa_files=()
    while IFS= read -r f; do
        siesa_files+=("$f")
    done < <(find "$APP_DIR/services" -type f -name "*.py" \
        ! -path "*/__pycache__/*" 2>/dev/null | \
        grep -E "(connekta|siesa|sync|packing|recepcion|muelle|inventario_siesa|devolucion|traslado)" | \
        sort)

    # Rutas relacionadas con operaciones Siesa
    while IFS= read -r f; do
        siesa_files+=("$f")
    done < <(find "$APP_DIR/routes" -type f -name "*.py" \
        ! -path "*/__pycache__/*" 2>/dev/null | \
        grep -E "(packing|recepcion|muelle|siesa|traslado|picking|devolucion)" | \
        sort)

    # Modelos de datos relevantes para integración
    while IFS= read -r f; do
        siesa_files+=("$f")
    done < <(find "$APP_DIR/models" -type f -name "*.py" \
        ! -path "*/__pycache__/*" 2>/dev/null | \
        grep -E "(packing|recepcion|pedido|siesa_job|siesa_mapeo|traslado|devolucion|bulto|inventario)" | \
        sort)

    for archivo in "${siesa_files[@]}"; do
        [ -r "$archivo" ] || continue

        # Mismo arreglo que en build_context: se proyecta, no se mira el
        # acumulado. Antes el contexto de siesa terminaba en 196 KB contra una
        # cota de 150 KB.
        local current_size file_size restante
        current_size=$(wc -c < "$ctx_file" 2>/dev/null || echo 0)
        file_size=$(wc -c < "$archivo" 2>/dev/null || echo 0)
        restante=$(( max_chars - current_size ))
        if [ "$file_size" -gt "$restante" ]; then
            # Recortado, no salteado: ver el motivo en build_context.
            if [ "$restante" -gt 20000 ]; then
                printf '=== ARCHIVO (RECORTADO a %d de %d bytes): %s ===\n' \
                    "$restante" "$file_size" "${archivo#$PROYECTO_DIR/}" >> "$ctx_file"
                head -c "$restante" "$archivo" >> "$ctx_file"
                printf '\n=== [CORTE — el resto del archivo no entra] ===\n' >> "$ctx_file"
            fi
            printf '\n=== [TRUNCADO: límite de %d bytes alcanzado] ===\n' "$max_chars" >> "$ctx_file"
            break
        fi

        printf '=== ARCHIVO: %s ===\n' "${archivo#$PROYECTO_DIR/}" >> "$ctx_file"
        cat "$archivo" >> "$ctx_file"
        printf '\n' >> "$ctx_file"
    done
}

# ── Determinar modo: completo o incremental ───────────────────
MODO="completo"
if [ "${SOLO_CAMBIOS:-false}" = "true" ] && git -C "$PROYECTO_DIR" rev-parse --git-dir &>/dev/null; then
    MODO="incremental (últimos 10 commits)"
    # Modo incremental: solo archivos modificados recientemente
    # Nota: los contextos por agente igual se filtran, pero se prioriza código cambiado
    warn "Modo incremental: se analizan todos los archivos pero se priorizan los del diff reciente"
fi

log "Modo: ${BOLD}$MODO${NC} | Modelo: ${BOLD}$CLAUDE_MODEL${NC}"

# ── Construir contextos por agente ───────────────────────────
log "Construyendo contextos de código por agente..."

CTX_BUGS="$TEMP_DIR/ctx_bugs.txt"
CTX_SECURITY="$TEMP_DIR/ctx_security.txt"
CTX_PERF="$TEMP_DIR/ctx_perf.txt"
CTX_SIESA="$TEMP_DIR/ctx_siesa.txt"
CTX_DEBT="$TEMP_DIR/ctx_debt.txt"

# bugs: servicios + modelos (lógica de negocio y datos)
build_context "$CTX_BUGS" "$MAX_CHARS_SERVICES" \
    "$APP_DIR/services" \
    "$APP_DIR/models"
# Dominio + adaptadores: la logica de negocio y los datos de flota.
append_flota "$CTX_BUGS" "$FLOTA_DIR/dominio" "$FLOTA_DIR/adaptadores"

# invariants: bugs context + últimas migraciones (para verificar DB constraints reales)
CTX_INVARIANTS="$TEMP_DIR/ctx_invariants.txt"
cp "$CTX_BUGS" "$CTX_INVARIANTS"
MIGRATIONS_DIR="$PROYECTO_DIR/migrations/versions"
if [ -d "$MIGRATIONS_DIR" ]; then
    printf '\n\n=== MIGRACIONES DE BASE DE DATOS (últimas 25) ===\n' >> "$CTX_INVARIANTS"
    # Las más recientes primero (orden cronológico inverso por fecha de modificación)
    while IFS= read -r mig; do
        current_size=$(wc -c < "$CTX_INVARIANTS" 2>/dev/null || echo 0)
        if [ "$current_size" -ge "$MAX_CHARS_ALL" ]; then break; fi
        printf '=== MIGRACIÓN: %s ===\n' "${mig#$PROYECTO_DIR/}" >> "$CTX_INVARIANTS"
        cat "$mig" >> "$CTX_INVARIANTS"
        printf '\n' >> "$CTX_INVARIANTS"
    done < <(find "$MIGRATIONS_DIR" -name "*.py" ! -path "*/__pycache__/*" 2>/dev/null \
        | sort -t_ -k1 -r | head -25)
fi

# security: rutas + configuración + extensiones
build_context "$CTX_SECURITY" "$MAX_CHARS_ROUTES" \
    "$APP_DIR/routes" \
    "$APP_DIR/__init__.py" \
    "$APP_DIR/extensions.py" \
    "$PROYECTO_DIR/config.py"
# Los blueprints de flota tienen sus propios @jwt_required y sus propias listas
# de roles: es donde ya aparecieron tres 403 que ninguna pantalla esperaba.
append_flota "$CTX_SECURITY" "$FLOTA_DIR/api"

# performance: servicios + modelos + rutas (necesita ver todo el stack)
build_context "$CTX_PERF" "$MAX_CHARS_ALL" \
    "$APP_DIR/services" \
    "$APP_DIR/models" \
    "$APP_DIR/routes"
MAX_CHARS_FLOTA=$MAX_CHARS_FLOTA_CORTO append_flota "$CTX_PERF" "$FLOTA_DIR/dominio"

# siesa_logic: solo archivos de integración (contexto enfocado y profundo)
build_siesa_context "$CTX_SIESA" "$MAX_CHARS_SIESA"

# tech_debt: todo el código (necesita visión global)
build_context "$CTX_DEBT" "$MAX_CHARS_ALL" "$APP_DIR"
MAX_CHARS_FLOTA=$MAX_CHARS_FLOTA_CORTO append_flota "$CTX_DEBT" "$FLOTA_DIR/dominio"

# Contar archivos totales analizados
NUM_ARCHIVOS=$(find "$APP_DIR" "$FLOTA_DIR" -name "*.py" ! -path "*/__pycache__/*" ! -path "*/venv/*" ! -path "*/migrations/*" 2>/dev/null | wc -l | tr -d ' ')

# ── Inyectar known_issues en cada prompt ────────────────────
KNOWN_ISSUES="$REVIEW_DIR/known_issues.json"
if [ -f "$KNOWN_ISSUES" ]; then
    KNOWN_ISSUES_SECTION=$(cat <<'KIEOF'

════════════════════════════════════════
ISSUES YA EVALUADOS — NO RE-REPORTAR
════════════════════════════════════════

Los siguientes patrones ya fueron evaluados y tienen mitigación documentada.
NO los incluyas en tu respuesta. Si crees que la mitigación es insuficiente,
explica ESPECÍFICAMENTE qué gap queda DESPUÉS de la mitigación — no repitas
el issue original.

KIEOF
)
    KNOWN_ISSUES_SECTION="$KNOWN_ISSUES_SECTION
$(python3 -c "
import json
with open('$KNOWN_ISSUES') as f:
    ki = json.load(f)
for d in ki.get('dismissed', []):
    print(f\"DISMISSED: {d['pattern']}\")
    print(f\"  Reason: {d['reason']}\")
    print()
for a in ki.get('accepted_risk', []):
    print(f\"ACCEPTED RISK: {a['pattern']}\")
    print(f\"  Reason: {a['reason']}\")
    print()
")"
fi

log "Contextos listos | Archivos totales del proyecto: ${BOLD}$NUM_ARCHIVOS${NC}"
for ctx in "$CTX_BUGS" "$CTX_SECURITY" "$CTX_PERF" "$CTX_SIESA" "$CTX_DEBT" "$CTX_INVARIANTS"; do
    name=$(basename "$ctx" .txt | sed 's/ctx_//')
    size=$(wc -c < "$ctx" | tr -d ' ')
    log "  [$name] → ${size} bytes"
done

# ── Función para correr un agente ────────────────────────────
run_agent() {
    local agent_name="$1"
    local prompt_file="$2"
    local ctx_file="$3"
    local output_file="$TEMP_DIR/${agent_name}.json"

    log "🚀 Iniciando agente: ${BOLD}$agent_name${NC}"

    # Construir input: prompt + código
    local input_file="$TEMP_DIR/${agent_name}_input.txt"
    cat "$prompt_file" > "$input_file"

    # Inject known issues
    if [ -n "$KNOWN_ISSUES_SECTION" ]; then
        printf '\n%s\n' "$KNOWN_ISSUES_SECTION" >> "$input_file"
    fi

    printf '\n\n' >> "$input_file"
    cat "$ctx_file" >> "$input_file"

    # Llamar a Claude Code, con un reintento.
    #
    # Nueve peticiones simultáneas de 200-380 KB se atropellan: en la corrida
    # del 2026-08-03 dos agentes murieron sin escribir nada y dos devolvieron el
    # stub de fallo. El reintento cubre el caso transitorio.
    local resultado="" intento fallo=1
    for intento in 1 2; do
        if resultado=$(claude --print --model "$CLAUDE_MODEL" < "$input_file" 2>>"$LOG_FILE"); then
            fallo=0
            break
        fi
        warn "Agente $agent_name falló (intento $intento/2)"
        sleep 5
    done

    if [ "$fallo" -eq 1 ]; then
        # **Un agente que falla no puede parecer un agente limpio.** Antes caía
        # al mismo stub que un parseo fallido —0 issues, score 0— y en el HTML
        # eso se lee como "no encontró problemas". Es lo contrario: no miró.
        python3 - "$agent_name" > "$output_file" <<'FAILEOF'
import json, sys
print(json.dumps({
    "agent": sys.argv[1],
    "issues": [],
    "score": None,
    "failed": True,
    "summary": "AGENTE NO EJECUTADO — la llamada al modelo falló dos veces. "
               "Esto NO significa que no haya problemas: significa que nadie "
               "los buscó. Revisar el log.",
}, ensure_ascii=False))
FAILEOF
        err "Agente ${BOLD}$agent_name${NC} NO SE EJECUTÓ — marcado como fallido"
        return 0
    fi

    # Extraer JSON de la respuesta
    local json_limpio
    json_limpio=$(echo "$resultado" | python3 "$EXTRACT_PY" "$agent_name" 2>>"$LOG_FILE" \
        || echo "{\"agent\":\"$agent_name\",\"issues\":[],\"summary\":\"Error de parsing\",\"score\":0}")

    echo "$json_limpio" > "$output_file"

    local num_issues
    num_issues=$(python3 -c "import sys,json; d=json.load(open('$output_file')); print(len(d.get('issues',[])))" 2>/dev/null || echo "?")
    local score
    score=$(python3 -c "import sys,json; d=json.load(open('$output_file')); print(d.get('score',0))" 2>/dev/null || echo "?")

    ok "Agente ${BOLD}$agent_name${NC} completado — ${BOLD}$num_issues${NC} issues | score: ${BOLD}$score${NC}"
}

# ── Ejecutar todos los agentes en PARALELO ───────────────────
echo ""
log "Ejecutando ${BOLD}9 agentes en paralelo${NC}..."
echo ""

# Lista de agentes: nombre | prompt | contexto
AGENTES=(
  "bugs|01_bugs.md|$CTX_BUGS"
  "security|02_security.md|$CTX_SECURITY"
  "performance|03_performance.md|$CTX_PERF"
  "siesa_logic|04_siesa_logic.md|$CTX_SIESA"
  "tech_debt|05_tech_debt.md|$CTX_DEBT"
  "siesa_spec|05_siesa_spec.md|$CTX_SIESA"
  "patterns|06_patterns.md|$CTX_DEBT"
  "resilience|07_resilience.md|$CTX_PERF"
  "invariants|08_invariants.md|$CTX_INVARIANTS"
)

# Se lanzan de a CONCURRENCIA, no los nueve de golpe.
#
# Con nueve simultaneos y contextos de 200-380 KB, el 2026-08-03 murieron dos
# agentes sin escribir nada y otros dos devolvieron el stub de fallo. Tarda mas
# y termina, que es la unica velocidad que sirve.
lanzados=0
for entrada in "${AGENTES[@]}"; do
    IFS="|" read -r nombre prompt ctx <<< "$entrada"
    run_agent "$nombre" "$PROMPTS_DIR/$prompt" "$ctx" &
    lanzados=$(( lanzados + 1 ))
    if [ "$(( lanzados % CONCURRENCIA ))" -eq 0 ]; then
        wait || warn "un agente del lote termino con error — se sigue"
    fi
done
wait || warn "un agente del ultimo lote termino con error — se sigue"

# Red de seguridad: si un agente murio tan temprano que no escribio su JSON, el
# merge fallaria por archivo inexistente y se perderia TODO el resto.
for entrada in "${AGENTES[@]}"; do
    a="${entrada%%|*}"
    if [ ! -s "$TEMP_DIR/${a}.json" ]; then
        err "Agente ${BOLD}$a${NC} no dejo resultado — se marca como no ejecutado"
        python3 - "$a" > "$TEMP_DIR/${a}.json" <<'MISSEOF'
import json, sys
print(json.dumps({
    "agent": sys.argv[1], "issues": [], "score": None, "failed": True,
    "summary": "AGENTE NO EJECUTADO — murio sin escribir resultado. "
               "Cero issues aca significa que nadie miro, no que este limpio.",
}, ensure_ascii=False))
MISSEOF
    fi
done

echo ""
log "Todos los agentes completados. Generando reporte..."

# ── Calcular fecha de reporte anterior (para detectar issues nuevos) ──
PREV_DATE_MAC=$(date -v-1d +%Y-%m-%d 2>/dev/null || echo "none")
PREV_DATE_GNU=$(date -d "yesterday" +%Y-%m-%d 2>/dev/null || echo "none")
PREV_DATE="${PREV_DATE_MAC:-$PREV_DATE_GNU}"
PREV_REPORT="$REPORTS_DIR/${PREV_DATE}_wms_review.json"
[ -f "$PREV_REPORT" ] || PREV_REPORT="none"

# ── Generar reporte HTML ──────────────────────────────────────
REPORTE_HTML="$REPORTS_DIR/${FECHA}_wms_review.html"
REPORTE_JSON="$REPORTS_DIR/${FECHA}_wms_review.json"

python3 "$REVIEW_DIR/merge_report.py" \
    --bugs        "$TEMP_DIR/bugs.json" \
    --security    "$TEMP_DIR/security.json" \
    --performance "$TEMP_DIR/performance.json" \
    --siesa       "$TEMP_DIR/siesa_logic.json" \
    --debt        "$TEMP_DIR/tech_debt.json" \
    --siesa-spec  "$TEMP_DIR/siesa_spec.json" \
    --patterns    "$TEMP_DIR/patterns.json" \
    --resilience  "$TEMP_DIR/resilience.json" \
    --invariants  "$TEMP_DIR/invariants.json" \
    --output-html "$REPORTE_HTML" \
    --output-json "$REPORTE_JSON" \
    --proyecto    "WMS-PAME-1" \
    --fecha       "$FECHA $HORA" \
    --modo        "$MODO" \
    --archivos    "$NUM_ARCHIVOS" \
    --prev-report "$PREV_REPORT"

# ── Enviar por email (opcional) ───────────────────────────────
if [ -f "$REPORTE_HTML" ] && [ -f "$REVIEW_DIR/send_email.py" ]; then
    log "Enviando reporte por email..."

    CRITICOS=$(python3 -c "
import json
with open('$REPORTE_JSON') as f:
    data = json.load(f)
print(sum(1 for i in data.get('all_issues', []) if i.get('severity') == 'CRÍTICO'))
" 2>/dev/null || echo "?")

    SUBJECT="🤖 WMS-PAME Review ${FECHA} — ${CRITICOS} CRÍTICOS"

    if python3 "$REVIEW_DIR/send_email.py" --subject "$SUBJECT" --html-file "$REPORTE_HTML"; then
        ok "Email enviado: $SUBJECT"
    else
        warn "No se pudo enviar el email. Reporte disponible en: $REPORTE_HTML"
    fi
else
    [ -f "$REVIEW_DIR/send_email.py" ] || warn "send_email.py no configurado. Ver setup en: $REVIEW_DIR/"
fi

# ── Limpieza ──────────────────────────────────────────────────
rm -rf "$TEMP_DIR"

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}  ✅ Review completado${NC}"
echo -e "  📄 HTML:  $REPORTE_HTML"
echo -e "  📊 JSON:  $REPORTE_JSON"
echo -e "  📋 Log:   $LOG_FILE"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
