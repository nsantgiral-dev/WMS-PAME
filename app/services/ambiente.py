"""
A qué base le estamos escribiendo. Y por qué el sistema no lo puede saber solo.

El 19 de agosto de 2026 el Gestor de Cartera pasó **ocho horas mostrando
cartera de QA creyendo que era producción**. Las cuatro comprobaciones
técnicas dieron en verde. La causa estaba dos capas por debajo de todo lo que
se puede medir desde la API: el Módulo de conectividad de Connekta tenía la
conexión SQL de **producción** apuntando a `SUnoEE_Papeleriamed_Imple`.

    integrador.siesacloud.com     (Connekta PRODUCCIÓN)  →  ..._Imple
    integradorqa.siesacloud.com   (Connekta QA)          →  ..._Imple

Nada en la respuesta de la API separa un original de su copia. Una base de QA
es una copia: mismo esquema, misma compañía, mismos maestros, y sigue
recibiendo escrituras.

> **Ningún sistema puede verificar su propio ambiente desde adentro.**
> El ambiente no se detecta: se declara y se contrasta contra el mundo.

## Por qué acá pesa más que allá

El Gestor **leía**. Ocho horas de números falsos se arreglaron cambiando la
conexión: la pantalla se corrigió sola.

El WMS **escribe**: remisiones que descargan inventario, facturas
electrónicas, recibos de caja, notas crédito, ajustes. Un documento escrito en
la base equivocada no se corrige cambiando la conexión — queda escrito, hay
que reversarlo uno por uno, y para entonces la mercancía ya salió del CD.

## Lo que este módulo NO hace

No adivina el ambiente. No hay forma. Hace tres cosas distintas:

1. **`comparar_credenciales()`** — el tamiz. Le pregunta lo mismo a las dos
   credenciales y compara. Si vuelven idénticas, hay **una sola base detrás**
   de dos hosts. Es lo que probó el caso del Gestor: 4.601 filas idénticas
   con dos ConniKey y dos tokens distintos.
2. **`declarar_contraste()`** — el único dato que vale: una persona con
   nombre cuadró una cifra del WMS contra algo de afuera.
3. **`estado()`** — junta las dos y **arranca en alarma**.

## El default es alarma, y ese es el punto

> El 19 de agosto no sonó nada en ocho horas justamente porque nadie había
> declarado nada, y el silencio se leyó como conformidad.

Sin declaración no hay «estado neutro»: hay una pregunta sin hacer. Es la
misma regla que ya rige en este repo para `siesa_rc_triggered` ausente y para
la cuarta columna de la reconciliación — no saber no se pinta igual que estar
bien.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Consulta del tamiz. Tiene que ser **de solo lectura** y sobre algo que
#: cambie a diario: el catálogo de ítems es casi igual en una copia reciente y
#: daría un falso «misma base». Los documentos de venta no — producción recibe
#: movimientos que la copia no tiene desde el día en que se copió.
_API_TAMIZ = 'API_v2_Ventas_Pedidos'

#: Se publica en las dos salidas de `estado()`. Escrita una vez: es la frase
#: que impide leer el host como evidencia, y en dos copias divergiría.
_ADVERTENCIA = (
    'El host y la compañía NO distinguen producción de una copia: una base de '
    'QA es una copia de producción y trae los dos iguales. Lo único que vale '
    'acá es el contraste declarado.')


def _huella(filas) -> str:
    """Hash estable del contenido. Ordenado, para que el orden de la respuesta
    no produzca diferencias que no existen."""
    crudo = json.dumps(filas, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(crudo.encode()).hexdigest()[:16]


def _credenciales_alternas():
    """El otro juego, si está configurado. Devuelve `None` si no."""
    url = (os.getenv('CONNEKTA_URL_ALTERNA') or '').strip()
    ikey = (os.getenv('CONNEKTA_IKEY_ALTERNA') or '').strip()
    itoken = (os.getenv('CONNEKTA_ITOKEN_ALTERNA') or '').strip()
    if not all([url, ikey, itoken]):
        return None
    return {'url': url, 'ikey': ikey, 'itoken': itoken}


def comparar_credenciales() -> dict:
    """Le pregunta lo mismo a los dos juegos de credenciales y compara.

    **Solo lee.** No escribe un documento ni toca inventario.

    El veredicto tiene tres valores y ninguno es «todo bien»:

    · `MISMA_BASE`   — filas idénticas con dos hosts, dos ConniKey y dos
      tokens. Es el caso del Gestor. **Bloquea el corte.**
    · `BASES_DISTINTAS` — el contenido difiere. Descarta *esta* falla, y
      **ninguna otra**: sigue sin decir cuál de las dos es producción.
    · `NO_SE_PUDO`   — falta configuración o una consulta falló. No es verde.
    """
    from app.services.connekta_gateway import connekta

    alterna = _credenciales_alternas()
    if not alterna:
        return {
            'veredicto': 'NO_SE_PUDO',
            'motivo': (
                'No hay credenciales alternas configuradas. Poner '
                'CONNEKTA_URL_ALTERNA, CONNEKTA_IKEY_ALTERNA y '
                'CONNEKTA_ITOKEN_ALTERNA con el juego del OTRO ambiente — el '
                'tamiz compara los dos, no puede correr con uno.'),
        }
    if connekta.modo_simulacion:
        return {'veredicto': 'NO_SE_PUDO',
                'motivo': 'El gateway está en modo simulación: no hay nada que comparar.'}

    params = {'paginacion': 'numPag=1|tamPag=100'}

    def _consultar(url, ikey, itoken):
        import requests
        r = requests.get(
            f'{url.rstrip("/")}/api/siesa/v3/ejecutarconsultaestandar',
            headers={'ConniKey': ikey, 'ConniToken': itoken,
                     'Content-Type': 'application/json'},
            params={**params, 'idCompania': connekta.id_compania,
                    'descripcion': _API_TAMIZ},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get('detalle', {}).get('Table', []) or []

    try:
        actual = _consultar(connekta.base_url, connekta.ikey, connekta.itoken)
        otra = _consultar(alterna['url'], alterna['ikey'], alterna['itoken'])
    except Exception as e:
        # **No se degrada a verde.** Una comparación que no corrió no
        # descarta nada; leerla como «distintas» sería exactamente el
        # adaptador que responde lo que se espera oír.
        logger.error('[AMBIENTE] el tamiz de credenciales falló: %s', e)
        return {'veredicto': 'NO_SE_PUDO', 'motivo': f'La consulta falló: {e}'}

    h_actual, h_otra = _huella(actual), _huella(otra)
    identicas = h_actual == h_otra and len(actual) > 0

    return {
        'veredicto': 'MISMA_BASE' if identicas else 'BASES_DISTINTAS',
        'consulta': _API_TAMIZ,
        'actual': {'host': connekta.base_url, 'filas': len(actual), 'huella': h_actual},
        'alterna': {'host': alterna['url'], 'filas': len(otra), 'huella': h_otra},
        'motivo': (
            'Dos hosts, dos ConniKey y dos tokens devolvieron EXACTAMENTE las '
            'mismas filas. Hay una sola base de datos detrás de los dos '
            'ambientes — es el caso del Gestor de Cartera del 2026-08-19. '
            'NO mover CONNEKTA_URL hasta que el proveedor confirme por escrito '
            'el destino de la conexión SQL.'
            if identicas else
            'El contenido difiere, así que no son la misma base. Eso descarta '
            'ESA falla y ninguna otra: no dice cuál de las dos es producción.'),
    }


def _config_actual() -> dict:
    """Lo que define el ambiente desde el punto de vista de la configuración.
    Si esto cambia, cualquier declaración anterior deja de valer."""
    from app.services.connekta_gateway import connekta
    return {'host': connekta.base_url, 'id_compania': str(connekta.id_compania),
            'id_cia_siesa': str(connekta.id_cia_siesa)}


def huella_config() -> str:
    return _huella(_config_actual())


def declarar_contraste(usuario_id: int, concepto: str, cifra_wms: str,
                       cifra_externa: str, fuente_externa: str,
                       notas: str = None):
    """Una persona con nombre declara que cuadró una cifra contra el mundo.

    `fuente_externa` es obligatoria y **no puede ser el propio WMS**: el error
    que este módulo existe para impedir es medir el sistema contra sí mismo.
    La consistencia interna la produce el código, no los datos.
    """
    from app.extensions import db
    from app.models.declaracion_ambiente import DeclaracionAmbiente

    for campo, valor in (('concepto', concepto), ('cifra_wms', cifra_wms),
                         ('cifra_externa', cifra_externa),
                         ('fuente_externa', fuente_externa)):
        if not (valor or '').strip():
            raise ValueError(
                f'{campo} es obligatorio: una declaración sin qué se cuadró, '
                f'contra qué y con qué números no se puede auditar después.')

    cfg = _config_actual()
    d = DeclaracionAmbiente(
        declarado_por=usuario_id,
        declarado_en=datetime.now(timezone.utc).replace(tzinfo=None),
        huella_config=_huella(cfg),
        host=cfg['host'],
        id_compania=cfg['id_compania'],
        concepto=concepto.strip(),
        cifra_wms=str(cifra_wms).strip(),
        cifra_externa=str(cifra_externa).strip(),
        fuente_externa=fuente_externa.strip(),
        notas=(notas or '').strip() or None,
    )
    db.session.add(d)
    db.session.commit()
    logger.info('[AMBIENTE] contraste declarado por usuario=%s: %s',
                usuario_id, concepto)
    return d


def estado() -> dict:
    """El estado del ambiente. **Arranca en ALARMA.**

    Tres niveles, y el default no es el bueno:

    · `ALARMA`   — nadie declaró nada, o la configuración cambió desde la
      última declaración, o el tamiz dice que hay una sola base.
    · `DECLARADO` — alguien con nombre cuadró contra una fuente externa, con
      la configuración de hoy.
    """
    from app.models.declaracion_ambiente import DeclaracionAmbiente

    cfg = _config_actual()
    huella = _huella(cfg)

    # La tabla puede no existir: entre que el deploy arranca y `flask db
    # upgrade` corre, o si la migración falló. **Ese hueco tiene que dar
    # ALARMA, no un 500 con traza** — un error de servidor es lo que un
    # operador aprende a ignorar, y la franja del dashboard necesita un
    # motivo que se pueda leer.
    try:
        ultima = (DeclaracionAmbiente.query
                  .order_by(DeclaracionAmbiente.declarado_en.desc()).first())
    except Exception as e:
        from app.extensions import db
        db.session.rollback()
        logger.error('[AMBIENTE] no se pudo leer las declaraciones: %s', e)
        return {
            'estado': 'ALARMA',
            'motivos': [
                'No se pudo leer el registro de declaraciones '
                f'({type(e).__name__}). Probablemente la migración '
                'm010declaracionambiente no se ha aplicado. Mientras no se '
                'pueda leer, no hay contraste declarado — y eso es alarma, '
                'no un problema técnico menor.'],
            'config': cfg,
            'huella_config': huella,
            'advertencia': _ADVERTENCIA,
            'ultima_declaracion': None,
        }

    motivos = []
    if ultima is None:
        motivos.append(
            'Nadie ha declarado un contraste contra una fuente externa. La '
            'ausencia de comprobación no es un estado tranquilo: es una '
            'pregunta sin hacer.')
    elif ultima.huella_config != huella:
        motivos.append(
            f'La configuración cambió desde la última declaración '
            f'({ultima.declarado_en:%Y-%m-%d}): entonces era host={ultima.host} '
            f'compañía={ultima.id_compania}, hoy es host={cfg["host"]} '
            f'compañía={cfg["id_compania"]}. La declaración anterior no dice '
            f'nada sobre el ambiente de ahora.')

    return {
        'estado': 'ALARMA' if motivos else 'DECLARADO',
        'motivos': motivos,
        'config': cfg,
        'huella_config': huella,
        #: El host y la compañía se publican **como lo que son**: config, no
        #: evidencia. Las dos las hereda una copia — son la primera y la
        #: segunda de las cuatro comprobaciones que no distinguen nada.
        'advertencia': _ADVERTENCIA,
        'ultima_declaracion': ultima.to_dict() if ultima else None,
    }
