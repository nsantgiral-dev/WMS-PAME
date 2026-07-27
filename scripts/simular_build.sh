#!/usr/bin/env bash
# Simula el ARBOL y el COMANDO del build de Railway. NO el interprete.
#
# El nombre importa: "simular_build" prometeria simular el build entero, y no
# lo hace. Cubre exactamente la causa de los tres fallos que lo originaron
# —que archivos llegan al contenedor— y NO cubre la divergencia que sigue
# viva: aqui corre un Python y alla otro, con dependencias flotantes encima.
# Una herramienta construida para acabar con las promesas de mas no puede
# hacer una.
#
# Existe porque tres builds seguidos murieron por diferencias entre mi máquina y
# el contenedor de despliegue, no por el código:
#   1. .gitignore se tragó un script → el archivo no estaba en el repo
#   2. el test verificaba Path.exists() contra el DISCO, no contra el repo
#   3. el test corregido usaba `git ls-files` — y el contenedor NO TIENE .git
#
# Railway copia el ÁRBOL DE ARCHIVOS que git entrega, no el repositorio. Por eso
# la simulación usa `git archive`: reproduce exactamente lo que llega allá.
# Lo que no está commiteado, no aparece. Que es justo el punto.
#
# Uso:  bash scripts/simular_build.sh
# Sale 0 si el build pasaría, 1 si no.
set -u
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIM="$(mktemp -d)"
trap 'rm -rf "$SIM"' EXIT

cd "$RAIZ" || exit 1
echo "  Simulando el contenedor de build (árbol de git, sin .git)…"
git archive HEAD | tar -x -C "$SIM" || { echo "  git archive falló"; exit 1; }

CMD=$(grep -E '^\s*buildCommand' railway.toml | sed 's/.*buildCommand *= *"//; s/"$//')
if [ -z "$CMD" ]; then echo "  no se encontró buildCommand en railway.toml"; exit 1; fi
echo "  $CMD"
echo

cd "$SIM" || exit 1
PY="$RAIZ/venv/bin/python"
[ -x "$PY" ] || PY=python
eval "${CMD/#python/$PY}"
RC=$?

echo
PYVER=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
echo "  ── Alcance de esta simulación ─────────────────────────────────"
echo "     SÍ cubre:  qué archivos llegan (git archive), sin .git,"
echo "                y el buildCommand exacto de railway.toml."
echo "     NO cubre:  el intérprete ni las versiones de dependencias."
echo "                Aquí: Python $PYVER. En Railway: sin fijar en nixpacks."
echo "                Mientras Python y las dependencias no estén pineados,"
echo "                un verde aquí NO garantiza un verde allá."
echo

if [ $RC -eq 0 ]; then
  echo "  ÁRBOL Y COMANDO: PASARÍAN."
  echo "  (No es garantía de build: falta equivalencia de intérprete.)"
else
  echo "  ÁRBOL Y COMANDO: FALLARÍAN — no pushees todavía."
  echo "  Si pasa en tu máquina y falla aquí, la diferencia es lo que NO está"
  echo "  commiteado o lo que sólo existe en tu entorno."
fi
exit $RC
