"""
El sync de catálogo contra `API_v2_Items.docx`, campo por campo.

La Regla 1 dice «leer el DOCX del conector antes de codificar». El sync de
catálogo (`siesa_sync_service`) nunca se cruzó contra el suyo, y leía **dos
campos que el contrato no declara**:

    f120_ind_estado                   → no existe
    f120_id_unidad_medida_inventario  → el nombre real es f120_id_unidad_inventario

Lo que cada uno costaba, y por qué no es código muerto:

1. **El sync REACTIVABA lo que un admin desactivaba.** `f120_ind_estado` no
   viene nunca, así que `.get(..., '1')` devolvía siempre el default y
   `activo` quedaba en `True`. En el update eso se escribía sobre el producto.
   Del otro lado, `productos.py::eliminar_producto` pone `activo = False` —
   un admin desactivando a mano. **Cada corrida del sync deshacía esa
   decisión**, sin log y sin aviso. Un default que revierte al operario no es
   el lado conservador de la Regla 0: es el destructivo.

2. **Todo el catálogo nacía en 'UND'.** El campo leído no existe, así que
   `unidad_medida` caía siempre al default fijo aunque Siesa trajera CJA, PAQ
   o RES. Que fue un dedazo y no una decisión lo prueba el propio archivo: los
   otros tres campos de unidad (`f120_id_unidad_empaque`,
   `f120_id_unidad_adicional`, `f120_id_unidad_orden`) sí están bien
   nombrados. Se equivocó solo en ése. Y agrava el defecto de doble unidad
   (misma referencia en PQ y UND, `despacho_parcial_service.py:102-106`): si
   la unidad del catálogo es siempre UND, son indistinguibles por ese campo.

3. **`os` no estaba importado** y se usa en el `sleep` entre páginas. Con
   ≥100 filas en la página 1 el sync levantaba `NameError`, el `except`
   general hacía rollback y la corrida terminaba con error tras **una sola
   página**. El catálogo real son 28.000+ ítems: 27.900 nunca se sincronizaron
   y el estado del sync decía «error», no «incompleto».

## Los dos sentidos del detector

Cada defecto se prueba en las dos direcciones: que el detector **dispare**
sobre la operación rota y que **NO dispare** sobre la sana. Un detector que
solo prueba que dispara prueba la mitad — es la lección de los seis guards en
verde sobre propiedades que la vía sana satisfacía por construcción.

## El trinquete

`TestElContratoMandaSobreElCodigo` lee el `.docx` en cada corrida y exige que
todo `f120_*` que el código lee esté declarado ahí, o esté en `LEIDOS_SIN_
CONTRATO` con su motivo escrito. Es **por AST, no por texto**: los detectores
de texto se atraparon en sus propios docstrings siete veces en una semana, y
este archivo nombra los dos campos rotos varias veces en su propia prosa.
"""
import ast
import re
import unittest.mock as mock
import zipfile
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parents[1]
_SPEC = _RAIZ / 'docs' / 'siesa-specs' / 'API_v2_Items.docx'
_MODULO = _RAIZ / 'app' / 'services' / 'siesa_sync_service.py'


def _campos_del_contrato() -> set:
    """Los `f120_*` que declara `API_v2_Items.docx`.

    Se lee el `.docx` en cada corrida en vez de copiar la lista acá: una copia
    diverge del original, y la divergencia entre dos copias de la misma verdad
    ya costó bastante en este repo.
    """
    assert _SPEC.exists(), f'falta el spec {_SPEC.name}'
    xml = zipfile.ZipFile(_SPEC).read('word/document.xml').decode('utf-8', 'ignore')
    return set(re.findall(r'f120_[a-z0-9_]+', re.sub(r'<[^>]+>', ' ', xml)))


def _campos_que_lee_el_codigo(fuente: str) -> dict:
    """`{campo: línea}` de cada literal `'f120_*'` del módulo, por AST.

    Por AST y no por regex a propósito. Este mismo archivo escribe
    `f120_ind_estado` en su docstring; un detector de texto sobre el módulo se
    atraparía igual en el suyo — ya pasó siete veces en una semana, la séptima
    con el regex que medía justamente esta regla.
    """
    campos = {}
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            if nodo.value.startswith('f120_'):
                campos.setdefault(nodo.value, nodo.lineno)
    return campos


#: Campos `f120_*` que el código nombra y el contrato NO declara, cada uno con
#: el motivo verificado de por qué se queda. Sin esta lista el trinquete no
#: podría distinguir un alias defensivo de un dedazo — que es exactamente lo
#: que pasó con `f120_id_unidad_medida_inventario`.
#:
#: Regla para agregar acá: hay que poder decir **qué escribe ese campo y qué
#: pasa cuando no viene**. Si la respuesta es «cae a un default que se
#: escribe», no va en esta lista: va arreglado.
LEIDOS_SIN_CONTRATO = {
    'f120_ind_estado':
        'No existe en el contrato. Se lee SOLO para saber si por casualidad '
        'viene (un conector personalizado podría agregarlo) y para contar '
        'cuántas filas no lo traen. Ausente NO escribe nada: `activo` queda '
        'como está. Ver TestElSyncNoRevierteAlOperario.',
    'f120_codigo_barras':
        'Alias defensivo. El código de barras real lo sincroniza '
        'siesa_barcode_sync_service contra API_v2_ItemsBarras (f131_*). '
        'Ausente deja `codigo_barras` en None y el update no lo escribe.',
    'f120_id_barras': 'Mismo alias defensivo que f120_codigo_barras.',
    'f120_barras': 'Mismo alias defensivo que f120_codigo_barras.',
}


class TestElContratoMandaSobreElCodigo:
    """Trinquete: ningún `f120_*` leído sin estar declarado o declarado aparte.

    Este es el denominador. «Arreglé dos campos» no dice cuántos quedan; esta
    clase cruza TODOS los que el módulo lee contra los 31 del contrato.
    """

    def test_todo_campo_que_el_codigo_lee_esta_en_el_contrato(self):
        contrato = _campos_del_contrato()
        leidos = _campos_que_lee_el_codigo(_MODULO.read_text(encoding='utf-8'))
        huerfanos = {c: ln for c, ln in leidos.items()
                     if c not in contrato and c not in LEIDOS_SIN_CONTRATO}
        assert not huerfanos, (
            f'\n{_MODULO.name} lee campos que API_v2_Items.docx NO declara:\n' +
            ''.join(f'  {c}  (línea {ln})\n' for c, ln in sorted(huerfanos.items())) +
            '\nUn campo inexistente no da error: `.get()` devuelve el default, '
            'y si ese default se escribe, el sync pisa el dato bueno con uno '
            'inventado. Así es como el catálogo entero nació en UND.\n'
            'O el nombre está mal (cruzarlo contra el DOCX), o hay una razón '
            'para leerlo igual y va en LEIDOS_SIN_CONTRATO con su motivo.')

    def test_el_contrato_declara_los_31_campos_que_se_cruzaron(self):
        """Piso mínimo: un parser roto devuelve pocos campos y aprueba todo.

        Si el `.docx` cambia de formato y el regex deja de encontrar campos,
        el conjunto vacío hace pasar el test de arriba sobre cualquier cosa.
        """
        contrato = _campos_del_contrato()
        assert len(contrato) == 31, (
            f'el contrato dio {len(contrato)} campos, no 31 — o cambió el '
            f'spec (rever el cruce entero) o se rompió el parser')

    def test_la_lista_de_excepciones_no_tiene_entradas_rancias(self):
        """Un campo que el contrato SÍ declara no puede seguir excusado.

        Y uno que el código ya no lee tampoco: la excusa quedaría afirmando
        una situación que no existe.
        """
        contrato = _campos_del_contrato()
        leidos = _campos_que_lee_el_codigo(_MODULO.read_text(encoding='utf-8'))
        ya_declarados = [c for c in LEIDOS_SIN_CONTRATO if c in contrato]
        assert not ya_declarados, (
            f'{ya_declarados} están en el contrato — sacarlos de la lista')
        sin_uso = [c for c in LEIDOS_SIN_CONTRATO if c not in leidos]
        assert not sin_uso, (
            f'{sin_uso} ya no se leen — sacarlos de la lista')

    def test_los_dos_campos_del_defecto_no_pueden_volver_por_la_lista(self):
        """La salida fácil del trinquete es excusar el campo en vez de arreglarlo.

        `f120_id_unidad_medida_inventario` no existe y su nombre real sí — no
        hay motivo que lo justifique. Y `f120_ind_estado` puede leerse, pero
        nunca con un default: eso es lo que revertía al operario.
        """
        assert 'f120_id_unidad_medida_inventario' not in LEIDOS_SIN_CONTRATO
        fuente = _MODULO.read_text(encoding='utf-8')
        assert 'f120_id_unidad_medida_inventario' not in _campos_que_lee_el_codigo(fuente)
        assert 'f120_id_unidad_inventario' in _campos_que_lee_el_codigo(fuente)

    def test_el_trinquete_dispara_sobre_un_campo_inventado(self):
        """El detector en el otro sentido: que vea una violación construida.

        Sin esto, «0 huérfanos» podría significar «el detector no mira nada».
        """
        fuente_rota = (
            "def f(row):\n"
            "    return row.get('f120_campo_que_no_existe', '1')\n")
        contrato = _campos_del_contrato()
        leidos = _campos_que_lee_el_codigo(fuente_rota)
        huerfanos = [c for c in leidos if c not in contrato
                     and c not in LEIDOS_SIN_CONTRATO]
        assert huerfanos == ['f120_campo_que_no_existe']

    def test_el_trinquete_no_dispara_sobre_los_campos_buenos(self):
        """Y que NO vea violación donde no la hay."""
        fuente_sana = (
            "def f(row):\n"
            "    return (row.get('f120_referencia'),\n"
            "            row.get('f120_descripcion'),\n"
            "            row.get('f120_id_unidad_inventario'),\n"
            "            row.get('f120_id_unidad_empaque'))\n")
        contrato = _campos_del_contrato()
        leidos = _campos_que_lee_el_codigo(fuente_sana)
        assert [c for c in leidos if c not in contrato] == []

    def test_el_detector_es_por_AST_y_no_por_texto(self):
        """Un docstring que nombra un campo no es una lectura de ese campo.

        Este archivo nombra `f120_id_unidad_medida_inventario` varias veces en
        su prosa. Un detector de texto sobre el módulo se atraparía igual en el
        suyo — la forma exacta que ya costó siete veces.
        """
        fuente = (
            '"""Este módulo NO lee f120_campo_en_docstring."""\n'
            "CONSTANTE = 1  # ni f120_campo_en_comentario\n")
        assert _campos_que_lee_el_codigo(fuente) == {}


# ---------------------------------------------------------------------------
# El sync corriendo de verdad
# ---------------------------------------------------------------------------

def _correr_sync(app, filas, filas_pag2=None):
    """Corre `_run_sync` con páginas mockeadas y devuelve el resultado.

    Se llama al servicio real, no se insertan filas a mano: un arnés que
    escribe `Producto(...)` directo solo prueba que la base acepta esas filas.
    """
    from flask import current_app
    from app.services import siesa_sync_service as svc

    paginas = {1: filas, 2: filas_pag2 or []}

    def fake_get_items(pagina=1):
        return {'detalle': {'Table': paginas.get(pagina, [])}}

    with mock.patch.object(svc.connekta, 'get_items_catalogo',
                           side_effect=fake_get_items):
        svc._run_sync(current_app._get_current_object())
    return svc._sync_estado['ultimo_resultado']


#: Una fila como las que Siesa manda de verdad: exactamente los campos del
#: contrato, sin `f120_ind_estado` ni `f120_id_unidad_medida_inventario`.
def _fila_real(referencia, **extra):
    fila = {
        'f120_id_cia': 1,
        'f120_referencia': referencia,
        'f120_descripcion': f'Descripción de {referencia}',
        'f120_id_tipo_inv_serv': 'INV',
        'f120_id_unidad_inventario': 'UND',
        'f120_id_unidad_empaque': 'UND',
        'f120_ind_tipo_item': 1,
    }
    fila.update(extra)
    return fila


class TestElSyncNoRevierteAlOperario:
    """Consecuencia 1: el sync deshacía la desactivación manual de un admin."""

    def test_un_producto_desactivado_a_mano_sigue_desactivado(self, app, db):
        """El defecto, con los datos reales: la fila NO trae el campo.

        `productos.py::eliminar_producto` pone `activo = False`. Cada corrida
        del sync lo devolvía a `True` porque `.get('f120_ind_estado', '1')`
        contestaba el default. Sin log y sin aviso.
        """
        from app.models.producto import Producto

        db.session.add(Producto(codigo='REF-OFF', nombre='Descontinuado',
                                codigo_siesa='REF-OFF', activo=False,
                                unidad_medida='UND'))
        db.session.commit()

        _correr_sync(app, [_fila_real('REF-OFF')])

        prod = Producto.query.filter_by(codigo_siesa='REF-OFF').first()
        assert prod.activo is False, (
            'el sync reactivó un producto que un admin desactivó a mano — '
            'la fuente no trae estado, así que no hay nada que sincronizar')

    def test_un_producto_activo_sigue_activo(self, app, db):
        """El otro sentido: no dispara sobre operación sana.

        Dejar de tocar `activo` no puede desactivar nada.
        """
        from app.models.producto import Producto

        db.session.add(Producto(codigo='REF-ON', nombre='Vigente',
                                codigo_siesa='REF-ON', activo=True,
                                unidad_medida='UND'))
        db.session.commit()

        _correr_sync(app, [_fila_real('REF-ON')])

        assert Producto.query.filter_by(codigo_siesa='REF-ON').first().activo is True

    def test_un_alta_nueva_nace_activa(self, app, db):
        """Un producto que no existe todavía no tiene decisión que respetar.

        No hay nada que revertir en un alta: nace activo, como siempre. Lo que
        cambia es que ya no se pisa lo que un humano decidió después.
        """
        from app.models.producto import Producto

        _correr_sync(app, [_fila_real('REF-NUEVA')])

        prod = Producto.query.filter_by(codigo_siesa='REF-NUEVA').first()
        assert prod is not None
        assert prod.activo is True

    def test_si_la_fuente_trae_el_estado_se_respeta(self, app, db):
        """Dato presente no es dato ausente.

        El contrato no lo declara, pero si un conector personalizado lo
        agregara, ese SÍ es dato de la fuente y manda. La Regla 0 gobierna el
        hueco, no lo que llegó.
        """
        from app.models.producto import Producto

        db.session.add(Producto(codigo='REF-BAJA', nombre='Se da de baja',
                                codigo_siesa='REF-BAJA', activo=True,
                                unidad_medida='UND'))
        db.session.commit()

        _correr_sync(app, [_fila_real('REF-BAJA', f120_ind_estado='0')])

        assert Producto.query.filter_by(codigo_siesa='REF-BAJA').first().activo is False

    def test_la_corrida_declara_cuantas_filas_vinieron_sin_estado(self, app, db):
        """Silenciar sin contar es exactamente lo que hizo invisible el defecto.

        El resultado del sync tiene que poder responder «¿cuántos productos no
        traen estado?». Hoy la respuesta es «todos», y eso es un hecho medido
        por corrida, no una suposición del docstring.
        """
        resultado = _correr_sync(app, [
            _fila_real('REF-S1'),
            _fila_real('REF-S2'),
            _fila_real('REF-S3', f120_ind_estado='1'),
        ])

        assert resultado['sin_estado_en_origen'] == 2, (
            'el sync no cuenta las filas que llegan sin f120_ind_estado — '
            'sin ese número, «no se sincroniza el estado» es una creencia')
        assert resultado['total_procesados'] == 3


class TestLaUnidadSaleDelContrato:
    """Consecuencia 2: todo el catálogo nacía en 'UND'."""

    def test_un_alta_toma_la_unidad_que_manda_siesa(self, app, db):
        from app.models.producto import Producto

        _correr_sync(app, [_fila_real('REF-CJA',
                                      f120_id_unidad_inventario='CJA')])

        prod = Producto.query.filter_by(codigo_siesa='REF-CJA').first()
        assert prod.unidad_medida == 'CJA', (
            'el alta cayó al default fijo: el código leía '
            'f120_id_unidad_medida_inventario, que no existe. El nombre real '
            'del contrato es f120_id_unidad_inventario')

    def test_un_update_corrige_la_unidad_del_producto_existente(self, app, db):
        """Los productos ya creados en UND se arreglan solos en el próximo sync.

        Por eso no hay backfill ni migración: la corrección viaja por el mismo
        camino que trajo el error.
        """
        from app.models.producto import Producto

        db.session.add(Producto(codigo='REF-PAQ', nombre='Paquete',
                                codigo_siesa='REF-PAQ', activo=True,
                                unidad_medida='UND'))
        db.session.commit()

        _correr_sync(app, [_fila_real('REF-PAQ',
                                      f120_id_unidad_inventario='PAQ')])

        assert Producto.query.filter_by(
            codigo_siesa='REF-PAQ').first().unidad_medida == 'PAQ'

    def test_una_fila_sin_unidad_no_borra_la_que_ya_habia(self, app, db):
        """El otro sentido: dato ausente no pisa dato bueno (Regla 0)."""
        from app.models.producto import Producto

        db.session.add(Producto(codigo='REF-KEEP', nombre='Con unidad',
                                codigo_siesa='REF-KEEP', activo=True,
                                unidad_medida='RES'))
        db.session.commit()

        fila = _fila_real('REF-KEEP')
        del fila['f120_id_unidad_inventario']
        _correr_sync(app, [fila])

        assert Producto.query.filter_by(
            codigo_siesa='REF-KEEP').first().unidad_medida == 'RES'

    def test_un_alta_sin_unidad_sigue_naciendo_en_UND(self, app, db):
        """El default de alta no cambia — solo deja de aplicarse siempre."""
        from app.models.producto import Producto

        fila = _fila_real('REF-SINU')
        del fila['f120_id_unidad_inventario']
        _correr_sync(app, [fila])

        assert Producto.query.filter_by(
            codigo_siesa='REF-SINU').first().unidad_medida == 'UND'


class TestElRestoDelSyncNoCambia:
    """Detector en las dos direcciones, sobre los campos que NO se tocaron."""

    def test_un_alta_completa_mapea_todos_los_campos_como_antes(self, app, db):
        from app.models.producto import Producto
        from app.models.siesa_mapeo_unidades import SiesaMapeoUnidades

        db.session.add(SiesaMapeoUnidades(tipo_inv_siesa='INV',
                                          unidad_negocio_id='001',
                                          descripcion='Papelería'))
        db.session.commit()

        _correr_sync(app, [_fila_real(
            'REF-FULL',
            f120_descripcion='Cuaderno cosido 100 hojas',
            f120_id_unidad_inventario='UND',
            f120_id_unidad_empaque='CJA',
            f120_codigo_barras='7701234567890',
            f421_factor='12',
        )])

        prod = Producto.query.filter_by(codigo_siesa='REF-FULL').first()
        assert prod.codigo == 'REF-FULL'
        assert prod.nombre == 'Cuaderno cosido 100 hojas'
        assert prod.clasificacion_abc == 'C'
        assert prod.unidad_negocio_id == '001'
        assert prod.codigo_barras == '7701234567890'
        assert prod.unidad_empaque == 'CJA'
        assert prod.factor_conversion == 12

    def test_un_update_sigue_actualizando_nombre_y_empaque(self, app, db):
        from app.models.producto import Producto

        db.session.add(Producto(codigo='REF-UPD', nombre='Nombre viejo',
                                codigo_siesa='REF-UPD', activo=True,
                                unidad_medida='UND', unidad_empaque='UND',
                                factor_conversion=1))
        db.session.commit()

        resultado = _correr_sync(app, [_fila_real(
            'REF-UPD',
            f120_descripcion='Nombre nuevo',
            f120_id_unidad_empaque='CJA',
            f120_codigo_barras='7709999999999',
            f421_factor='24',
        )])

        prod = Producto.query.filter_by(codigo_siesa='REF-UPD').first()
        assert prod.nombre == 'Nombre nuevo'
        assert prod.unidad_empaque == 'CJA'
        assert prod.codigo_barras == '7709999999999'
        assert prod.factor_conversion == 24
        assert resultado['actualizados'] == 1
        assert resultado['creados'] == 0

    def test_una_fila_sin_referencia_se_omite(self, app, db):
        from app.models.producto import Producto

        antes = Producto.query.count()
        resultado = _correr_sync(app, [_fila_real('')])

        assert Producto.query.count() == antes
        assert resultado['total_procesados'] == 0
        assert resultado['sin_estado_en_origen'] == 0, (
            'una fila descartada por no traer referencia no es una fila sin '
            'estado — contarla daría una proporción que no cuadra con nada')

    def test_un_tipo_de_inventario_sin_mapeo_se_declara(self, app, db):
        resultado = _correr_sync(app, [_fila_real(
            'REF-SINMAP', f120_id_tipo_inv_serv='DESCONOCIDO')])
        assert resultado['tipos_sin_mapeo'] == ['DESCONOCIDO']


class TestLaPaginacionLlegaALaSegundaPagina:
    """El `sleep` entre páginas usaba `os` sin importarlo.

    Con ≥100 filas en la página 1 el sync levantaba `NameError`, el `except`
    general hacía rollback y la corrida moría **tras una sola página**. El
    catálogo real son 28.000+ ítems: el sync nunca pasó de los primeros 100, y
    el estado decía «error», no «incompleto» — nadie podía leer en ese mensaje
    que faltaba el 99,6% del catálogo.
    """

    def test_con_una_pagina_llena_el_sync_sigue_a_la_siguiente(self, app, db):
        from app.models.producto import Producto

        pag1 = [_fila_real(f'REF-P1-{i:03d}') for i in range(100)]
        pag2 = [_fila_real('REF-P2-001')]

        resultado = _correr_sync(app, pag1, filas_pag2=pag2)

        assert resultado is not None, (
            'el sync abortó antes de terminar — revisar `ultimo_error`')
        assert resultado['total_procesados'] == 101
        assert Producto.query.filter_by(codigo_siesa='REF-P2-001').first() is not None

    def test_una_pagina_corta_corta_la_paginacion(self, app, db):
        """El otro sentido: no se pide una página de más sobre catálogo sano."""
        resultado = _correr_sync(app, [_fila_real('REF-CORTA')])
        assert resultado['total_procesados'] == 1


@pytest.fixture(autouse=True)
def _limpiar_estado_del_sync():
    """El estado del sync es global — un test no puede leer el de otro."""
    from app.services import siesa_sync_service as svc
    svc._sync_estado.update({'en_curso': False, 'ultimo_inicio': None,
                             'ultimo_resultado': None, 'ultimo_error': None})
    yield
    svc._sync_estado.update({'en_curso': False, 'ultimo_inicio': None,
                             'ultimo_resultado': None, 'ultimo_error': None})
