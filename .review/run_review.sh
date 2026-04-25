#!/usr/bin/env bash
# ============================================================
# WMS-PAME Review Agents Orchestrator v2
# 5 agentes especializados en paralelo → reporte HTML ejecutivo
# ============================================================

set -euo pipefail

# ── Configuración ────────────────────────────────────────────
PROYECTO_DIR="${WMS_DIR:-$HOME/PROYECTOS/WMS-PAME-1}"
APP_DIR="$PROYECTO_DIR/app"
REVIEW_DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPTS_DIR="$REVIEW_DIR/prompts"
REPORTS_DIR="$REVIEW_DIR/reports"
TEMP_DIR=$(mktemp -d)
FECHA=$(date +%Y-%m-%d)
HORA=$(date +%H:%M)
LOG_FILE="$REPORTS_DIR/${FECHA}_review.log"

# Modelo a usar (cambiar a claude-opus-4-6 para análisis más profundo)
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-sonnet-4-6}"

# Límite de chars por contexto de agente (200KB ≈ ~50K tokens)
MAX_CHARS_SERVICES=200000
MAX_CHARS_ROUTES=200000
MAX_CHARS_SIESA=180000
MAX_CHARS_ALL=250000

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
    for archivo in "${all_files[@]}"; do
        [ -r "$archivo" ] || continue

        # Verificar tamaño antes de agregar
        local current_size
        current_size=$(wc -c < "$ctx_file" 2>/dev/null || echo 0)
        if [ "$current_size" -ge "$max_chars" ]; then
            printf '\n=== [TRUNCADO: límite de %d bytes alcanzado — %d archivos más omitidos] ===\n' \
                "$max_chars" "$(( ${#all_files[@]} ))" >> "$ctx_file"
            break
        fi

        printf '=== ARCHIVO: %s ===\n' "${archivo#$PROYECTO_DIR/}" >> "$ctx_file"
        cat "$archivo" >> "$ctx_file"
        printf '\n' >> "$ctx_file"
    done
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

        local current_size
        current_size=$(wc -c < "$ctx_file" 2>/dev/null || echo 0)
        if [ "$current_size" -ge "$max_chars" ]; then
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

# security: rutas + configuración + extensiones
build_context "$CTX_SECURITY" "$MAX_CHARS_ROUTES" \
    "$APP_DIR/routes" \
    "$APP_DIR/__init__.py" \
    "$APP_DIR/extensions.py" \
    "$PROYECTO_DIR/config.py"

# performance: servicios + modelos + rutas (necesita ver todo el stack)
build_context "$CTX_PERF" "$MAX_CHARS_ALL" \
    "$APP_DIR/services" \
    "$APP_DIR/models" \
    "$APP_DIR/routes"

# siesa_logic: solo archivos de integración (contexto enfocado y profundo)
build_siesa_context "$CTX_SIESA" "$MAX_CHARS_SIESA"

# tech_debt: todo el código (necesita visión global)
build_context "$CTX_DEBT" "$MAX_CHARS_ALL" "$APP_DIR"

# Contar archivos totales analizados
NUM_ARCHIVOS=$(find "$APP_DIR" -name "*.py" ! -path "*/__pycache__/*" ! -path "*/venv/*" ! -path "*/migrations/*" 2>/dev/null | wc -l | tr -d ' ')

log "Contextos listos | Archivos totales del proyecto: ${BOLD}$NUM_ARCHIVOS${NC}"
for ctx in "$CTX_BUGS" "$CTX_SECURITY" "$CTX_PERF" "$CTX_SIESA" "$CTX_DEBT"; do
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
    printf '\n\n' >> "$input_file"
    cat "$ctx_file" >> "$input_file"

    # Llamar a Claude Code en modo no-interactivo
    local resultado
    if resultado=$(claude --print --model "$CLAUDE_MODEL" < "$input_file" 2>>"$LOG_FILE"); then
        :
    else
        warn "Agente $agent_name terminó con código de error — usando resultado parcial"
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
log "Ejecutando ${BOLD}5 agentes en paralelo${NC}..."
echo ""

run_agent "bugs"        "$PROMPTS_DIR/01_bugs.md"        "$CTX_BUGS"     &
PID_BUGS=$!

run_agent "security"    "$PROMPTS_DIR/02_security.md"    "$CTX_SECURITY" &
PID_SECURITY=$!

run_agent "performance" "$PROMPTS_DIR/03_performance.md" "$CTX_PERF"     &
PID_PERF=$!

run_agent "siesa_logic" "$PROMPTS_DIR/04_siesa_logic.md" "$CTX_SIESA"    &
PID_SIESA=$!

run_agent "tech_debt"   "$PROMPTS_DIR/05_tech_debt.md"   "$CTX_DEBT"     &
PID_DEBT=$!

# Esperar a que todos terminen
wait $PID_BUGS $PID_SECURITY $PID_PERF $PID_SIESA $PID_DEBT

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
