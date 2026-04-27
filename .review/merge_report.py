#!/usr/bin/env python3
"""
WMS-PAME Review Report Merger v2
Combina los resultados de 9 agentes, deduplica issues cross-agente
y genera un reporte HTML ejecutivo.
"""
import json
import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path


# ── Helpers de severidad y agente ────────────────────────────

def severity_order(s):
    return {"CRÍTICO": 0, "ALTO": 1, "MEDIO": 2, "BAJO": 3}.get(s, 4)

def severity_color(s):
    return {
        "CRÍTICO": "#f85149",
        "ALTO":    "#db6d28",
        "MEDIO":   "#d29922",
        "BAJO":    "#3fb950",
    }.get(s, "#8b949e")

def severity_bg(s):
    return {
        "CRÍTICO": "rgba(248,81,73,0.12)",
        "ALTO":    "rgba(219,109,40,0.12)",
        "MEDIO":   "rgba(210,153,34,0.10)",
        "BAJO":    "rgba(63,185,80,0.10)",
    }.get(s, "rgba(139,148,158,0.1)")

def agent_icon(name):
    return {
        "bugs":        "🐛",
        "security":    "🔐",
        "performance": "⚡",
        "siesa_logic": "🔗",
        "tech_debt":   "🏗️",
        "siesa_spec":  "📋",
        "patterns":    "🔎",
        "resilience":  "💥",
        "invariants":  "⚖️",
    }.get(name, "🤖")

def agent_label(name):
    return {
        "bugs":        "Bug Hunter",
        "security":    "Security Scanner",
        "performance": "Performance Analyst",
        "siesa_logic": "SIESA Logic Validator",
        "tech_debt":   "Tech Debt Detector",
        "siesa_spec":  "SIESA Spec Validator",
        "patterns":    "Pattern Auditor",
        "resilience":  "Resilience Analyst",
        "invariants":  "Invariants & Observability",
    }.get(name, name.title())

def score_color(score):
    if score >= 8: return "#3fb950"
    if score >= 6: return "#d29922"
    if score >= 4: return "#db6d28"
    return "#f85149"

def score_label(score):
    if score >= 8: return "Bueno"
    if score >= 6: return "Regular"
    if score >= 4: return "Atención"
    return "Crítico"


# ── Carga segura de JSON ──────────────────────────────────────

def load_json(path):
    try:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠ Warning: No se pudo cargar {path}: {e}", file=sys.stderr)
    return {"agent": "unknown", "issues": [], "summary": "No disponible", "score": 0}


# ── Deduplicación cross-agente ────────────────────────────────

# Prioridad de agente cuando dos reportan el mismo issue (0 = mayor prioridad)
AGENT_PRIORITY = {
    "siesa_logic": 0,   # integración es lo más crítico para este negocio
    "siesa_spec":  1,   # conformidad contractual Connekta
    "bugs":        2,
    "resilience":  3,
    "invariants":  4,
    "security":    5,
    "patterns":    6,
    "performance": 7,
    "tech_debt":   8,
}

def _dedup_key(issue):
    """
    Clave de deduplicación: archivo + primeras 70 chars del título normalizado.
    Dos issues del mismo archivo con título similar se consideran el mismo problema.
    """
    file_part  = (issue.get("file") or "").strip().lower()
    title_part = re.sub(r"\s+", " ", (issue.get("title") or "").strip().lower())[:70]
    return f"{file_part}|{title_part}"


def _title_words(issue):
    """Conjunto de palabras significativas del título (>3 chars) para comparación semántica."""
    title = re.sub(r"\s+", " ", (issue.get("title") or "").strip().lower())
    return {w for w in re.split(r"[\s\-_/]+", title) if len(w) > 3}


def _is_similar_issue(issue, prev_issue, threshold=0.6):
    """
    True si issue y prev_issue son probablemente el mismo problema.
    Criterios: mismo archivo Y solapamiento de palabras significativas >= threshold.
    """
    if (issue.get("file") or "").strip().lower() != (prev_issue.get("file") or "").strip().lower():
        return False
    words_a = _title_words(issue)
    words_b = _title_words(prev_issue)
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b) / min(len(words_a), len(words_b))
    return overlap >= threshold

def deduplicate_issues(all_issues):
    """
    Elimina issues duplicados entre agentes.
    - Si dos agentes reportan el mismo issue: conserva el de mayor severidad.
    - Si misma severidad: conserva el del agente de mayor prioridad (siesa_logic > bugs > ...).
    - Devuelve la lista deduplicada y un dict {key: [agents]} con los agentes fusionados.
    """
    seen = {}          # key -> issue elegido
    merged_agents = {} # key -> set de agentes que reportaron este issue

    for issue in all_issues:
        key = _dedup_key(issue)
        agent = issue.get("_agent", "unknown")

        if key not in seen:
            seen[key] = issue
            merged_agents[key] = {agent}
        else:
            existing = seen[key]
            merged_agents[key].add(agent)

            # Elegir el issue con mayor severidad
            new_sev = severity_order(issue.get("severity", "BAJO"))
            old_sev = severity_order(existing.get("severity", "BAJO"))

            if new_sev < old_sev:
                # El nuevo tiene mayor severidad → reemplazar
                seen[key] = issue
            elif new_sev == old_sev:
                # Misma severidad → preferir por prioridad de agente
                new_prio = AGENT_PRIORITY.get(agent, 99)
                old_prio = AGENT_PRIORITY.get(existing.get("_agent", ""), 99)
                if new_prio < old_prio:
                    seen[key] = issue

    # Anotar los agentes que reportaron cada issue (para mostrar en el reporte)
    result = []
    for key, issue in seen.items():
        agents = merged_agents[key]
        issue = dict(issue)  # copia para no mutar el original
        if len(agents) > 1:
            issue["_also_reported_by"] = sorted(agents - {issue.get("_agent", "")})
        result.append(issue)

    return result


# ── Detección de issues nuevos vs reporte anterior ───────────

def _load_all_prev_issues(reports_dir: str) -> list:
    """
    Carga todos los issues de todos los reportes JSON previos en REPORTS_DIR.
    Esto evita que un issue marcado como 'NUEVO' solo porque no estaba en el de ayer.
    """
    all_prev = []
    if not reports_dir or not os.path.isdir(reports_dir):
        return all_prev
    for path in sorted(Path(reports_dir).glob("*_wms_review.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            all_prev.extend(data.get("all_issues", []))
        except Exception:
            pass
    return all_prev


def find_new_issues(current_issues, prev_data, reports_dir: str = None):
    """
    Un issue es NUEVO solo si no existe en NINGÚN reporte previo —
    ni por clave exacta ni por similitud semántica (mismo archivo, >60% palabras en común).
    """
    all_prev = _load_all_prev_issues(reports_dir)
    # También incluir issues del prev_data pasado explícitamente (backward compat)
    if prev_data:
        all_prev.extend(prev_data.get("all_issues", []))

    if not all_prev:
        return set()

    prev_keys = {_dedup_key(p) for p in all_prev}

    new_indices = set()
    for i, issue in enumerate(current_issues):
        if _dedup_key(issue) in prev_keys:
            continue
        # Comparación semántica: ¿hay algún issue previo similar?
        if any(_is_similar_issue(issue, p) for p in all_prev):
            continue
        new_indices.add(i)
    return new_indices


# ── Generación del HTML ───────────────────────────────────────

def generate_html(all_agents, args, prev_data=None):
    # 1. Recopilar todos los issues con su agente de origen
    raw_issues = []
    for agent_data in all_agents.values():
        for issue in agent_data.get("issues", []):
            issue = dict(issue)
            issue["_agent"] = agent_data.get("agent", "unknown")
            raw_issues.append(issue)

    # 2. Deduplicar cross-agente
    all_issues = deduplicate_issues(raw_issues)
    dedup_removed = len(raw_issues) - len(all_issues)

    # 3. Ordenar por severidad
    all_issues.sort(key=lambda x: severity_order(x.get("severity", "BAJO")))

    # 4. Estadísticas
    reports_dir = getattr(args, "reports_dir", None)
    new_issue_indices = find_new_issues(all_issues, prev_data, reports_dir=reports_dir)
    counts = {"CRÍTICO": 0, "ALTO": 0, "MEDIO": 0, "BAJO": 0}
    for issue in all_issues:
        s = issue.get("severity", "BAJO")
        counts[s] = counts.get(s, 0) + 1

    total_issues = len(all_issues)
    avg_score = round(
        sum(d.get("score", 0) for d in all_agents.values()) / max(len(all_agents), 1), 1
    )

    # Score ponderado por severidad: penaliza más los CRÍTICOS que los BAJOS
    # Fórmula: empieza en 10 y descuenta según severidad de los issues encontrados
    _SEV_PESO = {"CRÍTICO": 1.5, "ALTO": 0.8, "MEDIO": 0.3, "BAJO": 0.1}
    _penalizacion = sum(_SEV_PESO.get(iss.get("severity", "BAJO"), 0.1) for iss in all_issues)
    weighted_score = round(max(0.0, min(10.0, 10.0 - _penalizacion)), 1)

    new_count = len(new_issue_indices)

    # 5. Generar cards de issues
    issues_html = ""
    for i, issue in enumerate(all_issues):
        sev   = issue.get("severity", "BAJO")
        agent = issue.get("_agent", "")
        also  = issue.get("_also_reported_by", [])
        is_new = i in new_issue_indices

        # Campos opcionales según tipo de agente
        extra_fields = ""
        if issue.get("compliance_note"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">⚖️ Normativa</span>'
                f'<span class="field-value compliance">{issue["compliance_note"]}</span>'
                f'</div>'
            )
        if issue.get("estimated_impact"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">📊 Impacto estimado</span>'
                f'<span class="field-value">{issue["estimated_impact"]}</span>'
                f'</div>'
            )
        if issue.get("business_impact"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">💼 Impacto negocio</span>'
                f'<span class="field-value">{issue["business_impact"]}</span>'
                f'</div>'
            )
        if issue.get("effort_to_fix"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">⏱️ Esfuerzo</span>'
                f'<span class="field-value">{issue["effort_to_fix"]}</span>'
                f'</div>'
            )
        if issue.get("connector_id"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🔌 Conector</span>'
                f'<span class="field-value">{issue["connector_id"]}</span>'
                f'</div>'
            )
        if issue.get("produccion_impacto"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🏭 Impacto producción</span>'
                f'<span class="field-value">{issue["produccion_impacto"]}</span>'
                f'</div>'
            )
        if issue.get("pattern_id"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🔎 Patrón</span>'
                f'<span class="field-value" style="font-family:monospace">{issue["pattern_id"]}</span>'
                f'</div>'
            )
        if issue.get("missing_in"):
            missing_list = issue["missing_in"] if isinstance(issue["missing_in"], list) else [issue["missing_in"]]
            missing_html = ", ".join(f'<code style="font-size:11px">{m}</code>' for m in missing_list)
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">📍 Falta en</span>'
                f'<span class="field-value">{missing_html}</span>'
                f'</div>'
            )
        if issue.get("recovery_posible") is not None:
            recovery_val = issue["recovery_posible"]
            recovery_str = "Sí" if recovery_val else "No"
            recovery_color = "#3fb950" if recovery_val else "#f85149"
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🔄 Recovery posible</span>'
                f'<span class="field-value" style="color:{recovery_color};font-weight:600">{recovery_str}</span>'
                f'</div>'
            )
        if issue.get("tiempo_deteccion"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">⏰ Tiempo detección</span>'
                f'<span class="field-value">{issue["tiempo_deteccion"]}</span>'
                f'</div>'
            )
        if issue.get("datos_en_riesgo"):
            riesgo = issue["datos_en_riesgo"]
            riesgo_color = "#f85149" if riesgo in ("fiscal", "ambos") else "#db6d28" if riesgo == "inventario" else "#8b949e"
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">⚠️ Datos en riesgo</span>'
                f'<span class="field-value" style="color:{riesgo_color};font-weight:600">{riesgo}</span>'
                f'</div>'
            )
        if issue.get("estado_sistema_post_fallo"):
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">💾 Estado post-fallo</span>'
                f'<span class="field-value">{issue["estado_sistema_post_fallo"]}</span>'
                f'</div>'
            )
        if issue.get("tipo"):
            tipo_labels = {"invariant_violation": "Violación de invariante", "silent_failure": "Fallo silencioso"}
            tipo_colors = {"invariant_violation": "#D97706", "silent_failure": "#DC2626"}
            tipo_val = issue["tipo"]
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🏷️ Tipo</span>'
                f'<span class="field-value" style="color:{tipo_colors.get(tipo_val, "#8b949e")}">'
                f'{tipo_labels.get(tipo_val, tipo_val)}</span>'
                f'</div>'
            )
        if issue.get("detectable_en"):
            det = issue["detectable_en"]
            det_color = "#f85149" if det == "nunca" else "#db6d28" if det == "horas" else "#3fb950"
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🔭 Detectable en</span>'
                f'<span class="field-value" style="color:{det_color};font-weight:600">{det}</span>'
                f'</div>'
            )
        if issue.get("enforcement_actual"):
            enf_labels = {
                "solo_app_code": "Solo app code (insuficiente)",
                "solo_db": "Solo DB constraint",
                "ambos": "App code + DB constraint",
                "ninguno": "Sin enforcement",
            }
            enf_colors = {
                "solo_app_code": "#db6d28",
                "solo_db": "#d29922",
                "ambos": "#3fb950",
                "ninguno": "#f85149",
            }
            enf_val = issue["enforcement_actual"]
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🛡️ Enforcement actual</span>'
                f'<span class="field-value" style="color:{enf_colors.get(enf_val, "#8b949e")}">'
                f'{enf_labels.get(enf_val, enf_val)}</span>'
                f'</div>'
            )
        if also:
            also_tags = " ".join(
                f'<span class="agent-tag">{agent_icon(a)} {agent_label(a)}</span>'
                for a in also
            )
            extra_fields += (
                f'<div class="issue-field">'
                f'<span class="field-label">🔁 También detectado por</span>'
                f'<span class="field-value">{also_tags}</span>'
                f'</div>'
            )

        # Code snippet
        snippet = ""
        if issue.get("code_snippet"):
            escaped = (
                issue["code_snippet"]
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            snippet = f'<pre class="code-snippet"><code>{escaped}</code></pre>'

        new_badge   = '<span class="new-badge">NUEVO</span>' if is_new else ""
        dedup_badge = (
            '<span class="dedup-badge">FUSIONADO</span>'
            if also else ""
        )

        issues_html += f"""
        <div class="issue-card" data-severity="{sev}" data-agent="{agent}">
          <div class="issue-header" style="background:{severity_bg(sev)};border-left:4px solid {severity_color(sev)}">
            <div class="issue-header-left">
              <span class="severity-badge" style="background:{severity_color(sev)}">{sev}</span>
              {new_badge}{dedup_badge}
              <span class="agent-tag">{agent_icon(agent)} {agent_label(agent)}</span>
            </div>
            <div class="issue-title">{issue.get("title", "Sin título")}</div>
            <div class="issue-file">📄 {issue.get("file", "?")} — {issue.get("line_hint", "")}</div>
          </div>
          <div class="issue-body">
            <div class="issue-field">
              <span class="field-label">🔍 Descripción</span>
              <span class="field-value">{issue.get("description", "")}</span>
            </div>
            <div class="issue-field">
              <span class="field-label">✅ Recomendación</span>
              <span class="field-value rec">{issue.get("recommendation", "")}</span>
            </div>
            {extra_fields}
            {snippet}
          </div>
        </div>"""

    if not issues_html:
        issues_html = '<div class="no-issues">🎉 No se encontraron issues en esta revisión</div>'

    # 6. Score cards por agente
    agent_cards = ""
    for name, data in all_agents.items():
        score    = data.get("score", 0)
        n_issues = len(data.get("issues", []))
        agent_cards += f"""
        <div class="agent-score-card">
          <div class="agent-score-icon">{agent_icon(name)}</div>
          <div class="agent-score-name">{agent_label(name)}</div>
          <div class="agent-score-num" style="color:{score_color(score)}">{score}</div>
          <div class="agent-score-label" style="color:{score_color(score)}">{score_label(score)}</div>
          <div class="agent-score-issues">{n_issues} issues brutos</div>
          <div class="agent-summary">{data.get("summary", "")}</div>
        </div>"""

    # ── HTML completo ─────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WMS-PAME Code Review — {args.fecha}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  :root {{
    --bg:      #0d1117;
    --bg2:     #161b22;
    --bg3:     #1c2128;
    --border:  #30363d;
    --text:    #e6edf3;
    --text2:   #8b949e;
    --accent:  #58a6ff;
    --green:   #3fb950;
    --red:     #f85149;
    --yellow:  #d29922;
    --orange:  #db6d28;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: 'Inter', sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.6;
    padding: 24px;
  }}

  /* Header */
  .header {{
    background: linear-gradient(135deg, #1c2128 0%, #0d1117 100%);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 32px;
    margin-bottom: 24px;
    position: relative;
    overflow: hidden;
  }}
  .header::before {{
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: linear-gradient(90deg, #58a6ff, #3fb950, #d29922, #f85149);
  }}
  .header-title    {{ font-size: 24px; font-weight: 700; margin-bottom: 4px; }}
  .header-subtitle {{ color: var(--text2); font-size: 13px; }}
  .header-meta     {{
    display: flex; gap: 20px; margin-top: 16px; flex-wrap: wrap;
    color: var(--text2); font-size: 12px;
  }}
  .header-meta span {{ display: flex; align-items: center; gap: 4px; }}

  /* Score global */
  .global-score {{
    position: absolute; top: 32px; right: 32px;
    text-align: center; display: flex; gap: 20px;
  }}
  .score-block {{ text-align: center; }}
  .global-score-num {{
    font-size: 48px; font-weight: 700;
    line-height: 1;
  }}
  .global-score-label {{
    font-size: 11px; color: var(--text2); margin-top: 4px;
    text-transform: uppercase; letter-spacing: 1px;
  }}

  /* Stat cards */
  .stats {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }}
  .stat-card {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    text-align: center;
  }}
  .stat-num   {{ font-size: 32px; font-weight: 700; line-height: 1; }}
  .stat-label {{ font-size: 11px; color: var(--text2); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}

  /* Filters */
  .filters {{
    display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap;
    align-items: center;
  }}
  .filter-btn {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 14px;
    color: var(--text2);
    font-size: 12px;
    cursor: pointer;
    font-family: inherit;
    transition: all 0.15s;
  }}
  .filter-btn:hover, .filter-btn.active {{
    background: var(--bg3);
    color: var(--text);
    border-color: var(--accent);
  }}
  .filter-label {{ color: var(--text2); font-size: 12px; margin-right: 4px; }}

  /* Issue cards */
  .issue-card {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 12px;
    overflow: hidden;
    transition: border-color 0.15s;
  }}
  .issue-card:hover {{ border-color: #444d56; }}
  .issue-header {{
    padding: 12px 16px;
    cursor: pointer;
  }}
  .issue-header-left {{
    display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap;
  }}
  .severity-badge {{
    font-size: 10px; font-weight: 700;
    padding: 2px 8px; border-radius: 20px;
    color: white; text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .new-badge {{
    font-size: 10px; font-weight: 700;
    padding: 2px 8px; border-radius: 20px;
    background: #58a6ff; color: #0d1117;
    text-transform: uppercase;
  }}
  .dedup-badge {{
    font-size: 10px; font-weight: 700;
    padding: 2px 8px; border-radius: 20px;
    background: #8957e5; color: white;
    text-transform: uppercase;
  }}
  .agent-tag {{
    font-size: 11px; color: var(--text2);
    background: var(--bg3);
    padding: 2px 8px; border-radius: 4px;
  }}
  .issue-title {{ font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 4px; }}
  .issue-file  {{ font-size: 11px; color: var(--text2); font-family: 'JetBrains Mono', monospace; }}
  .issue-body  {{
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    background: rgba(0,0,0,0.2);
    display: none;
  }}
  .issue-card.open .issue-body {{ display: block; }}
  .issue-field  {{ margin-bottom: 10px; }}
  .field-label  {{
    display: block; font-size: 11px; color: var(--text2);
    text-transform: uppercase; letter-spacing: 0.5px;
    margin-bottom: 3px; font-weight: 600;
  }}
  .field-value            {{ font-size: 13px; color: var(--text); }}
  .field-value.rec        {{ color: #3fb950; }}
  .field-value.compliance {{ color: #d29922; }}
  pre.code-snippet {{
    background: #010409;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px; margin-top: 8px;
    overflow-x: auto;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px; line-height: 1.5; color: #e6edf3;
  }}

  /* Agent score cards */
  .agents-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }}
  .agent-score-card {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    text-align: center;
  }}
  .agent-score-icon   {{ font-size: 24px; margin-bottom: 6px; }}
  .agent-score-name   {{ font-size: 12px; color: var(--text2); margin-bottom: 8px; }}
  .agent-score-num    {{ font-size: 28px; font-weight: 700; line-height: 1; }}
  .agent-score-label  {{ font-size: 11px; font-weight: 600; margin-top: 2px; }}
  .agent-score-issues {{ font-size: 11px; color: var(--text2); margin-top: 4px; }}
  .agent-summary      {{ font-size: 11px; color: var(--text2); margin-top: 8px; text-align: left; line-height: 1.5; }}

  .section-title {{
    font-size: 16px; font-weight: 600;
    margin-bottom: 12px; color: var(--text);
    display: flex; align-items: center; gap: 8px;
  }}
  .section-title::after {{
    content: ''; flex: 1; height: 1px; background: var(--border);
  }}

  .no-issues {{ text-align: center; padding: 40px; color: var(--text2); font-size: 16px; }}

  .footer {{
    text-align: center; color: var(--text2); font-size: 11px;
    margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border);
  }}

  @media (max-width: 600px) {{
    body {{ padding: 12px; }}
    .global-score {{ position: static; margin-top: 16px; text-align: left; }}
    .global-score-num {{ font-size: 36px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-title">🤖 WMS-PAME — Code Review Report</div>
  <div class="header-subtitle">Análisis automatizado por 5 agentes especializados</div>
  <div class="header-meta">
    <span>📅 {args.fecha}</span>
    <span>📁 {args.archivos} archivos analizados</span>
    <span>🔄 Modo: {args.modo}</span>
    <span>🆕 {new_count} issues nuevos</span>
    <span>🔁 {dedup_removed} duplicados eliminados</span>
  </div>
  <div class="global-score">
    <div class="score-block">
      <div class="global-score-num" style="color:{score_color(avg_score)}">{avg_score}</div>
      <div class="global-score-label">Promedio agentes</div>
    </div>
    <div class="score-block">
      <div class="global-score-num" style="color:{score_color(weighted_score)}">{weighted_score}</div>
      <div class="global-score-label">Score ponderado</div>
    </div>
  </div>
</div>

<div class="stats">
  <div class="stat-card">
    <div class="stat-num" style="color:#f85149">{counts["CRÍTICO"]}</div>
    <div class="stat-label">🚨 Críticos</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" style="color:#db6d28">{counts["ALTO"]}</div>
    <div class="stat-label">🔴 Altos</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" style="color:#d29922">{counts["MEDIO"]}</div>
    <div class="stat-label">🟡 Medios</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" style="color:#3fb950">{counts["BAJO"]}</div>
    <div class="stat-label">🟢 Bajos</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" style="color:#58a6ff">{total_issues}</div>
    <div class="stat-label">📋 Total</div>
  </div>
  <div class="stat-card">
    <div class="stat-num" style="color:#58a6ff">{new_count}</div>
    <div class="stat-label">🆕 Nuevos</div>
  </div>
</div>

<div class="section-title">Scores por Agente</div>
<div class="agents-grid">
  {agent_cards}
</div>

<div class="section-title">Issues Encontrados ({total_issues} únicos)</div>

<div class="filters">
  <span class="filter-label">Filtrar:</span>
  <button class="filter-btn active" onclick="filterAll(this)">Todos ({total_issues})</button>
  <button class="filter-btn" onclick="filterSeverity('CRÍTICO', this)">🚨 Críticos ({counts["CRÍTICO"]})</button>
  <button class="filter-btn" onclick="filterSeverity('ALTO', this)">🔴 Altos ({counts["ALTO"]})</button>
  <button class="filter-btn" onclick="filterNew(this)">🆕 Nuevos ({new_count})</button>
  <button class="filter-btn" onclick="filterAgent('siesa_logic', this)">🔗 SIESA</button>
  <button class="filter-btn" onclick="filterAgent('security', this)">🔐 Security</button>
  <button class="filter-btn" onclick="filterAgent('bugs', this)">🐛 Bugs</button>
  <button class="filter-btn" onclick="toggleAll()">↕ Expandir todo</button>
</div>

<div id="issues-container">
{issues_html}
</div>

<div class="footer">
  Generado por WMS-PAME Review Agents v2 • {args.fecha} •
  5 agentes especializados • {dedup_removed} duplicados cross-agente eliminados
</div>

<script>
  // Toggle individual issue
  document.querySelectorAll('.issue-header').forEach(h => {{
    h.addEventListener('click', () => h.closest('.issue-card').classList.toggle('open'));
  }});

  // Abrir primer CRÍTICO por defecto
  const firstCrit = document.querySelector('.issue-card[data-severity="CRÍTICO"]');
  if (firstCrit) firstCrit.classList.add('open');

  let allExpanded = false;
  function toggleAll() {{
    allExpanded = !allExpanded;
    document.querySelectorAll('.issue-card').forEach(c => {{
      c.classList.toggle('open', allExpanded);
    }});
  }}

  function showAll() {{
    document.querySelectorAll('.issue-card').forEach(c => c.style.display = '');
  }}

  function filterAll(btn) {{
    setActive(btn);
    showAll();
  }}

  function filterSeverity(sev, btn) {{
    setActive(btn);
    document.querySelectorAll('.issue-card').forEach(c => {{
      c.style.display = c.dataset.severity === sev ? '' : 'none';
    }});
  }}

  function filterAgent(agent, btn) {{
    setActive(btn);
    document.querySelectorAll('.issue-card').forEach(c => {{
      c.style.display = c.dataset.agent === agent ? '' : 'none';
    }});
  }}

  function filterNew(btn) {{
    setActive(btn);
    document.querySelectorAll('.issue-card').forEach(c => {{
      c.style.display = c.querySelector('.new-badge') ? '' : 'none';
    }});
  }}

  function setActive(btn) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  }}
</script>
</body>
</html>"""

    return html, all_issues, weighted_score


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WMS-PAME Review Report Merger v2")
    parser.add_argument("--bugs")
    parser.add_argument("--security")
    parser.add_argument("--performance")
    parser.add_argument("--siesa")
    parser.add_argument("--debt")
    parser.add_argument("--siesa-spec")
    parser.add_argument("--patterns")
    parser.add_argument("--resilience")
    parser.add_argument("--invariants")
    parser.add_argument("--output-html", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--proyecto",    default="WMS-PAME")
    parser.add_argument("--fecha",       default=datetime.now().strftime("%Y-%m-%d %H:%M"))
    parser.add_argument("--modo",        default="completo")
    parser.add_argument("--archivos",    default="?")
    parser.add_argument("--prev-report",  default=None)
    parser.add_argument("--reports-dir",  default=None,
                        help="Directorio de reportes JSON previos para comparación histórica")
    args = parser.parse_args()
    # Inferir reports_dir del output-html path si no se pasa explícitamente
    if not args.reports_dir and args.output_html:
        args.reports_dir = str(Path(args.output_html).parent)

    # Cargar datos de cada agente
    all_agents = {
        "bugs":        load_json(args.bugs),
        "security":    load_json(args.security),
        "performance": load_json(args.performance),
        "siesa_logic": load_json(args.siesa),
        "tech_debt":   load_json(args.debt),
        "siesa_spec":  load_json(args.siesa_spec),
        "patterns":    load_json(args.patterns),
        "resilience":  load_json(args.resilience),
        "invariants":  load_json(args.invariants),
    }

    # Reporte anterior para detectar issues nuevos
    prev_data = None
    if args.prev_report and args.prev_report != "none" and os.path.exists(args.prev_report):
        prev_data = load_json(args.prev_report)

    # Generar HTML
    html, all_issues, weighted_score = generate_html(all_agents, args, prev_data)

    # Escribir HTML
    Path(args.output_html).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_html, "w", encoding="utf-8") as f:
        f.write(html)

    # Escribir JSON consolidado
    json_output = {
        "fecha":               args.fecha,
        "proyecto":            args.proyecto,
        "modo":                args.modo,
        "archivos_analizados": args.archivos,
        "all_issues":          all_issues,
        "agents": {
            name: {
                "score":        d.get("score", 0),
                "summary":      d.get("summary", ""),
                "issues_count": len(d.get("issues", [])),
            }
            for name, d in all_agents.items()
        },
        "stats": {
            "total":          len(all_issues),
            "criticos":       sum(1 for i in all_issues if i.get("severity") == "CRÍTICO"),
            "altos":          sum(1 for i in all_issues if i.get("severity") == "ALTO"),
            "medios":         sum(1 for i in all_issues if i.get("severity") == "MEDIO"),
            "bajos":          sum(1 for i in all_issues if i.get("severity") == "BAJO"),
            "weighted_score": weighted_score,
        },
    }
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(json_output, f, ensure_ascii=False, indent=2)

    raw_total = sum(len(d.get("issues", [])) for d in all_agents.values())
    dedup_removed = raw_total - len(all_issues)

    print(f"✅ HTML: {args.output_html}")
    print(f"✅ JSON: {args.output_json}")
    print(f"📊 Issues: {len(all_issues)} únicos ({raw_total} brutos, {dedup_removed} duplicados eliminados)")
    print(f"🚨 Críticos: {json_output['stats']['criticos']}")


if __name__ == "__main__":
    main()
