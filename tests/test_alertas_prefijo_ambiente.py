"""
Un correo de QA tiene que decir que es de QA. Todos, no los que se acuerden.

QA y producción comparten `ALERTA_EMAIL_DEST` (2026-09-22). Sin marca, una
alerta de QA llega idéntica a una de producción: o alguien persigue un problema
que no existe, o aprende a no abrir el asunto — y el día que es de producción
tampoco lo abre.

La política vive en UNA función, `alertas_service.prefijo_ambiente()`, y se
aplica en UNA puerta, `alertas_service.enviar_email`, la única que habla con
Resend. Este archivo prueba las dos cosas:

1. **La política** — `production` y ausente no llevan prefijo; cualquier otro
   ambiente lleva `[<nombre>] `. Se prueba sobre el payload que de verdad sale
   hacia Resend, no sobre la función suelta: un helper correcto que nadie
   llama es exactamente lo que ya costó en la Regla 5.

2. **La clase** — «un correo que sale sin pasar por la puerta». Un segundo
   `requests.post` a Resend, o un `smtplib`, mandaría sin prefijo y ningún
   test de la política lo vería. El trinquete lo busca **por AST** en `app/`,
   `flota/` (paquete propio, FUERA de `app/`), `scripts/` y los `.py` de la
   raíz.

## Lo que el detector NO ve — escrito para que nadie lo suponga cubierto

- Una URL de Resend armada en tiempo de ejecución (concatenada, leída de una
  variable de entorno). El detector busca la constante con el host.
- Un SDK de correo nuevo con otro nombre de import. Hoy solo se conoce
  `smtplib` y el paquete `resend`; uno nuevo hay que agregarlo acá.
"""
import ast
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent

#: La única puerta. (archivo relativo, función).
PUERTA = ('app/services/alertas_service.py', 'enviar_email')

#: Llamadores conocidos de la puerta. No son una lista blanca — pueden crecer
#: libremente —; son el piso que prueba que el escáner sigue viendo el repo.
_NOMBRES_PUERTA = {'enviar_email', '_enviar_email_con_dlq'}

_HOST_RESEND = 'api.resend.com'
_MODULOS_CORREO = {'smtplib', 'resend'}


# ── La política ──────────────────────────────────────────────────────────────

class TestPrefijoAmbiente:

    def test_production_no_lleva_prefijo(self, monkeypatch):
        from app.services.alertas_service import prefijo_ambiente
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'production')
        assert prefijo_ambiente() == ''

    def test_qa_lleva_prefijo(self, monkeypatch):
        from app.services.alertas_service import prefijo_ambiente
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        assert prefijo_ambiente() == '[QA] '

    def test_ausente_no_lleva_prefijo(self, monkeypatch):
        """Regla 0: en local, en un script, en un test, nadie declaró ambiente,
        y no se inventa uno."""
        from app.services.alertas_service import prefijo_ambiente
        monkeypatch.delenv('RAILWAY_ENVIRONMENT_NAME', raising=False)
        assert prefijo_ambiente() == ''

    def test_vacio_cuenta_como_ausente(self, monkeypatch):
        from app.services.alertas_service import prefijo_ambiente
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', '  ')
        assert prefijo_ambiente() == ''

    def test_otro_ambiente_usa_su_nombre(self, monkeypatch):
        from app.services.alertas_service import prefijo_ambiente
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'staging')
        assert prefijo_ambiente() == '[staging] '


class _Resp:
    status_code = 200
    text = '{"id": "x"}'

    def json(self):
        return {'id': 'x'}


@pytest.fixture
def resend_capturado(monkeypatch):
    """Resend configurado y el POST capturado. Devuelve la lista de payloads."""
    import requests

    monkeypatch.setenv('RESEND_API_KEY', 're_test')
    monkeypatch.setenv('ALERTA_EMAIL_DEST', 'alguien@ejemplo.com')
    enviados = []

    def _post(url, json=None, **kw):
        enviados.append({'url': url, **json})
        return _Resp()

    monkeypatch.setattr(requests, 'post', _post)
    return enviados


class TestElAsuntoQueSaleHaciaResend:
    """Sobre el payload real, no sobre el helper."""

    def test_qa_sale_con_prefijo(self, monkeypatch, resend_capturado):
        from app.services.alertas_service import enviar_email
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        assert enviar_email('Stock crítico', '<p>x</p>', 'x') is True
        assert resend_capturado[0]['subject'] == '[QA] Stock crítico'

    def test_production_sale_sin_prefijo(self, monkeypatch, resend_capturado):
        from app.services.alertas_service import enviar_email
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'production')
        enviar_email('Stock crítico', '<p>x</p>', 'x')
        assert resend_capturado[0]['subject'] == 'Stock crítico'

    def test_ausente_sale_sin_prefijo(self, monkeypatch, resend_capturado):
        from app.services.alertas_service import enviar_email
        monkeypatch.delenv('RAILWAY_ENVIRONMENT_NAME', raising=False)
        enviar_email('Stock crítico', '<p>x</p>', 'x')
        assert resend_capturado[0]['subject'] == 'Stock crítico'

    def test_el_wrapper_con_dlq_hereda_el_prefijo(self, monkeypatch,
                                                  resend_capturado):
        """El camino de casi todos los llamadores: no pone prefijo por su
        cuenta, lo recibe de la puerta."""
        from app.services.alertas_service import _enviar_email_con_dlq
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        _enviar_email_con_dlq('Ruta sin liquidar', '<p>x</p>', 'x', 'rezago')
        assert resend_capturado[0]['subject'] == '[QA] Ruta sin liquidar'

    def test_el_reporte_de_flota_hereda_el_prefijo(self, app, monkeypatch,
                                                   resend_capturado):
        """`flota/` vive fuera de `app/`: un grep acotado a `app/` no lo ve. Se
        prueba su correo por su propio camino."""
        from flota.adaptadores import reporte_semanal as rs
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'QA')
        monkeypatch.setattr(rs, 'reporte_encendido', lambda: True)
        monkeypatch.setenv(rs._VAR_DESTINATARIOS, 'flota@ejemplo.com')
        monkeypatch.setattr(rs, 'armar_reporte', lambda dia=None: {
            'desde': '2026-09-14', 'hasta': '2026-09-20',
            'inspecciones': {'completas': 0, 'incompletas': 0},
            'hallazgos_vencidos': [], 'documentos_por_vencer_30d': [],
            'dia_operativo': '2026-09-21', 'etiqueta': 'x'})
        rs.enviar_reporte_semanal()
        assert resend_capturado[0]['subject'].startswith('[QA] Flota')


# ── La clase: nadie manda correo sin pasar por la puerta ─────────────────────

def _archivos():
    for carpeta in ('app', 'flota', 'scripts'):
        yield from sorted((RAIZ / carpeta).rglob('*.py'))
    yield from sorted(RAIZ.glob('*.py'))


def _funcion_de(arbol):
    """nodo → nombre de la función que lo contiene (la más interna)."""
    duena = {}

    def _visitar(nodo, actual):
        for hijo in ast.iter_child_nodes(nodo):
            nombre = (hijo.name if isinstance(
                hijo, (ast.FunctionDef, ast.AsyncFunctionDef)) else actual)
            duena[hijo] = nombre
            _visitar(hijo, nombre)

    _visitar(arbol, None)
    return duena


def _llama_prefijo(expr) -> bool:
    return any(isinstance(n, ast.Call)
               and isinstance(n.func, (ast.Name, ast.Attribute))
               and (getattr(n.func, 'id', None) or getattr(n.func, 'attr', None))
               == 'prefijo_ambiente'
               for n in ast.walk(expr))


def escanear(fuente: str, archivo: str) -> dict:
    """Devuelve los sitios de envío de correo de un fuente.

    - `resend`:  constantes con el host de Resend, por (archivo, función).
    - `modulos`: imports de `smtplib` / `resend`.
    - `asuntos`: claves `'subject'` en un dict literal, con si el valor o la
      función que lo arma pasa por `prefijo_ambiente()`.
    - `llamadas_puerta`: llamadas a `enviar_email` / `_enviar_email_con_dlq`
      (el piso: si baja, el escáner dejó de ver el repo).
    """
    arbol = ast.parse(fuente)
    duena = _funcion_de(arbol)
    funciones = {n.name: n for n in ast.walk(arbol)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    out = {'resend': [], 'modulos': [], 'asuntos': [], 'llamadas_puerta': 0}

    for n in ast.walk(arbol):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and _HOST_RESEND in n.value):
            out['resend'].append((archivo, duena.get(n)))
        elif isinstance(n, ast.Import):
            out['modulos'] += [(archivo, a.name) for a in n.names
                               if a.name.split('.')[0] in _MODULOS_CORREO]
        elif isinstance(n, ast.ImportFrom) and n.module:
            if n.module.split('.')[0] in _MODULOS_CORREO:
                out['modulos'].append((archivo, n.module))
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == 'subject':
                    fn = duena.get(n)
                    # El prefijo puede ir en la expresión o antes, dentro de la
                    # misma función (`asunto = prefijo_ambiente() + asunto`).
                    cuerpo = funciones.get(fn)
                    con = _llama_prefijo(v) or (cuerpo is not None
                                                and _llama_prefijo(cuerpo))
                    out['asuntos'].append((archivo, fn, con))
        elif isinstance(n, ast.Call):
            nombre = (getattr(n.func, 'id', None)
                      or getattr(n.func, 'attr', None))
            if nombre in _NOMBRES_PUERTA:
                out['llamadas_puerta'] += 1
    return out


def _escanear_repo():
    total = {'resend': [], 'modulos': [], 'asuntos': [], 'llamadas_puerta': 0,
             'archivos': 0}
    for ruta in _archivos():
        rel = ruta.relative_to(RAIZ).as_posix()
        r = escanear(ruta.read_text(encoding='utf-8'), rel)
        for k in ('resend', 'modulos', 'asuntos'):
            total[k] += r[k]
        total['llamadas_puerta'] += r['llamadas_puerta']
        total['archivos'] += 1
    return total


class TestUnaSolaPuertaAResend:

    def test_solo_enviar_email_habla_con_resend(self):
        r = _escanear_repo()
        assert r['resend'] == [PUERTA], (
            f'Sitios que construyen un envío a Resend: {r["resend"]}. Todo '
            f'correo sale por {PUERTA[1]}() — es la que pone el prefijo de '
            f'ambiente. Un segundo POST manda sin él.')

    def test_nadie_importa_otra_via_de_correo(self):
        r = _escanear_repo()
        assert r['modulos'] == [], (
            f'Imports de correo fuera de la puerta: {r["modulos"]}. '
            f'Usar alertas_service.enviar_email.')

    def test_todo_asunto_pasa_por_el_prefijo(self):
        r = _escanear_repo()
        sin = [(a, f) for a, f, con in r['asuntos'] if not con]
        assert r['asuntos'], 'no se encontró ningún asunto: el escáner no ve la puerta'
        assert sin == [], f'Asuntos armados sin prefijo_ambiente(): {sin}'


class TestElDetectorSeMide:
    """Un escáner que se desincroniza devuelve cero, y un cero se lee igual que
    «acá no hay nada que hacer»."""

    def test_piso_de_archivos_y_llamadores(self):
        r = _escanear_repo()
        # 127 en app/ + 29 en flota/ + scripts/: si esto cae, el glob se rompió.
        assert r['archivos'] >= 250, r['archivos']
        # Medido el 2026-09-22: 23 llamadas a la puerta en 10 archivos.
        assert r['llamadas_puerta'] >= 20, r['llamadas_puerta']

    def test_marca_un_segundo_post_a_resend(self):
        fuente = (
            'import requests\n'
            'def avisar_rapido(a):\n'
            '    requests.post("https://api.resend.com/emails",\n'
            '                  json={"subject": a})\n')
        r = escanear(fuente, 'app/services/otro.py')
        assert r['resend'] == [('app/services/otro.py', 'avisar_rapido')]
        assert r['asuntos'] == [('app/services/otro.py', 'avisar_rapido', False)]

    def test_marca_smtplib_y_el_sdk(self):
        r = escanear('import smtplib\nfrom resend import Emails\n', 'x.py')
        assert r['modulos'] == [('x.py', 'smtplib'), ('x.py', 'resend')]

    def test_marca_la_puerta_si_pierde_el_prefijo(self):
        """La mutación de la propiedad, en memoria: la puerta real sin la línea
        del prefijo tiene que caer."""
        fuente = (RAIZ / PUERTA[0]).read_text(encoding='utf-8')
        linea = '    asunto = prefijo_ambiente() + asunto\n'
        assert linea in fuente, 'la mutación no encontró la línea que cree quitar'
        mutado = fuente.replace(linea, '')
        assert 'prefijo_ambiente() + asunto' not in mutado
        r = escanear(mutado, PUERTA[0])
        assert (PUERTA[0], PUERTA[1], False) in r['asuntos']

    def test_no_marca_lo_que_no_envia(self):
        fuente = (
            'from app.services.alertas_service import enviar_email\n'
            'def alerta():\n'
            '    log("Resend no configurado")\n'
            '    d = {"asunto": "x", "resend": "api"}\n'
            '    enviar_email(asunto="x", cuerpo_html="", cuerpo_texto="")\n')
        r = escanear(fuente, 'app/services/y.py')
        assert r['resend'] == [] and r['modulos'] == [] and r['asuntos'] == []
        assert r['llamadas_puerta'] == 1
