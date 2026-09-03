# Arneses de mutación de flota

**Un test que pasa no dice nada por sí solo.** Puede estar midiendo algo que la
vía sana satisface por construcción — hay seis casos fechados de eso en
`CLAUDE.md`, todos guards en verde sobre propiedades rotas. Lo que convierte un
test en trinquete es que **muera** cuando la propiedad se rompe.

Cada archivo de acá rompe una propiedad a la vez, corre los tests que deberían
verla, y exige rojo.

```bash
venv/bin/python scripts/mutacion/hallazgo.py     # 26 mutaciones
venv/bin/python scripts/mutacion/inspeccion.py
venv/bin/python scripts/mutacion/gastos.py
venv/bin/python scripts/mutacion/confianza.py
venv/bin/python scripts/mutacion/geo.py
venv/bin/python scripts/mutacion/taller.py
venv/bin/python scripts/mutacion/llantas.py
venv/bin/python scripts/mutacion/guard_huerfanas.py
venv/bin/python scripts/mutacion/checks_sqlite.py    # cada CHECK, contra SQLite
venv/bin/python scripts/mutacion/checks_postgres.py  # cada CHECK, contra el motor real
```

Restauran el archivo en `finally`. Si uno se interrumpe a la fuerza, revisá con
`git diff` antes de seguir.

## Por qué viven acá y no en un directorio temporal

Hasta el 2026-09-02 vivían en el scratchpad de la sesión que los escribió.
`docs/flota/ESTADO.md` los citaba como evidencia de «30 mutaciones, 30 muertas»
y «37 de 37» — **evidencia que nadie más podía leer y que desaparecía al cerrar
la sesión**. Un número de verificación cuyo respaldo no se puede reproducir es
una afirmación, no una medición. Lo encontró la auditoría, no quien los escribió.

Y por eso la raíz del repo se **deriva** de `__file__` en vez de escribirse a
mano: la versión anterior tenía la ruta absoluta de una máquina, así que la
evidencia solo se podía reproducir ahí. Media falla del mismo tipo.

## El modo de falla del propio arnés

**Un test que se salta sale con exit 0 igual que uno que pasó.** El arnés que
caza verdes falsos produjo uno el 2026-09-02: corría el subproceso con un `PATH`
recortado, `node` no existía, los tests que ejecutan el JS real se saltaban, y
dos mutaciones figuraron como «sobrevivientes» sin haberse juzgado nunca.

Por eso cada arnés **parsea la salida de pytest**, no solo el código de retorno,
y distingue tres resultados: *muerta*, *sobrevivió* y **no se juzgó**.
