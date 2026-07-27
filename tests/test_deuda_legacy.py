"""
Trinquete sobre la deuda legacy de SQLAlchemy.

No exige migrar las 224 llamadas: exige que NO CREZCAN. Igual que el guard de
endpoints huérfanos — pasa con lo que hay, revienta con lo nuevo.

Por qué importa más el trinquete que la migración: las llamadas legacy
funcionan y seguirán funcionando ("long term deprecation"). El daño real era
otro — 637 advertencias de una clase conocida hacían invisible cualquier
advertencia NUEVA y real. Eso se arregló declarando el filtro en pytest.ini.
Este archivo evita que la deuda siga creciendo mientras tanto.
"""
import os
import re

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Conteo congelado al 2026-07-27. Solo puede BAJAR.
# Migración completa a db.session.get(Modelo, id): tarde tranquila después del
# comité del 7-ago-2026. Anotada como deuda con fecha, no como intención.
MAX_QUERY_GET_APP = 224


def _contar(directorio):
    total, por_archivo = 0, {}
    for raiz, _dirs, archivos in os.walk(os.path.join(_RAIZ, directorio)):
        if '__pycache__' in raiz or 'venv' in raiz:
            continue
        for a in archivos:
            if not a.endswith('.py'):
                continue
            ruta = os.path.join(raiz, a)
            with open(ruta, encoding='utf-8') as f:
                n = len(re.findall(r'\.query\.get\(', f.read()))
            if n:
                por_archivo[os.path.relpath(ruta, _RAIZ)] = n
                total += n
    return total, por_archivo


class TestTrinqueteQueryGet:
    """`Modelo.query.get(id)` es legacy en SQLAlchemy 2.0."""

    def test_no_crece_la_deuda(self):
        total, por_archivo = _contar('app')
        assert total <= MAX_QUERY_GET_APP, (
            f'\nLa deuda legacy creció: {total} llamadas a .query.get() '
            f'(tope {MAX_QUERY_GET_APP}).\n'
            f'Usa db.session.get(Modelo, id) en el código nuevo.\n'
            + '\n'.join(f'  {k}: {v}' for k, v in
                        sorted(por_archivo.items(), key=lambda x: -x[1])[:8])
        )

    def test_si_bajo_hay_que_bajar_el_tope(self):
        """Anti-podredumbre: si se migraron llamadas, el tope debe seguirlas.

        Sin esto el trinquete se afloja solo — un tope que quedó muy por encima
        del real vuelve a permitir que la deuda crezca sin avisar.
        """
        total, _ = _contar('app')
        assert total >= MAX_QUERY_GET_APP - 10, (
            f'Quedan {total} llamadas y el tope sigue en {MAX_QUERY_GET_APP}. '
            f'Baja MAX_QUERY_GET_APP a {total} para que el trinquete siga apretando.'
        )

    def test_el_filtro_de_warnings_esta_declarado_con_su_razon(self):
        """Silenciar sin explicar es esconder. El filtro lleva motivo y fecha."""
        ini = open(os.path.join(_RAIZ, 'pytest.ini'), encoding='utf-8').read()
        assert 'LegacyAPIWarning' in ini
        assert 'REVISAR' in ini, 'el filtro debe declarar cuándo se revisa'
        assert 'db.session.get' in ini, 'debe decir cuál es el reemplazo'


class TestCanalDeAdvertenciasLegible:
    """El canal solo sirve si está callado cuando no pasa nada."""

    def test_no_se_silencia_todo_en_bloque(self):
        """Cambiar un canal ruidoso por uno sordo no es un arreglo."""
        ini = open(os.path.join(_RAIZ, 'pytest.ini'), encoding='utf-8').read()
        assert 'ignore::DeprecationWarning' not in ini
        assert not re.search(r'^\s*ignore\s*$', ini, re.M), \
            'un ignore sin clase silenciaría TODO'

    def test_la_clave_de_test_no_dispara_avisos_de_criptografia(self):
        """Con clave corta PyJWT avisa por cada token: 183 avisos de ruido."""
        conftest = open(os.path.join(_RAIZ, 'tests', 'conftest.py'),
                        encoding='utf-8').read()
        m = re.search(r"os\.environ\['SECRET_KEY'\]\s*=\s*'([^']+)'", conftest)
        assert m, 'no se encontró SECRET_KEY en conftest'
        assert len(m.group(1).encode()) >= 32, \
            'la clave de tests debe tener 32+ bytes (RFC 7518)'
