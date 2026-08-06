"""
Los tres números del kardex que decidían plata sin que nadie los pudiera mirar.

El kardex alimenta el ROP, la clasificación S-B, el pronóstico TSB y el armado
del contenedor. Tres de sus endpoints no tenían pantalla, y cada uno contestaba
una pregunta que alguien se hace antes de firmar una compra:

  · `reconstruir`   — ¿qué días estuvo agotado este SKU? Sin esa serie la
                      demanda queda CENSURADA: se mide sobre días en los que no
                      había qué vender y se lee como baja.
  · `reconciliar`   — ¿le creo al kardex? Un concepto sin clasificar es un
                      agujero que ningún modelo reporta.
  · `stock-diario`  — la evidencia del «+18% por 62 días sin stock» que la fila
                      de Reposición ya mostraba sin poder abrir.

**Y había una promesa rota:** dos pantallas decían, en rojo, «Correr
*Reconstruir stock diario* en Inventario › Datos». Ese botón no existía en
ninguna parte. Una instrucción que no se puede seguir enseña que los avisos del
sistema no llevan a ningún lado.
"""
from pathlib import Path

import pytest

_PWA = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa'


def _js(nombre):
    return (_PWA / nombre).read_text(encoding='utf-8')


def _cuerpo(js, firma):
    """El cuerpo de una función, hasta la que la sigue.

    Se corta por el siguiente `\nasync function` / `\nfunction` y no por un
    largo fijo: una ventana de N caracteres se rompe callada cuando el código
    crece —pasó con esta misma suite— y el test empieza a proteger menos de lo
    que su nombre dice.
    """
    i = js.index(firma)
    resto = js[i + len(firma):]
    cortes = [c for c in (resto.find('\nasync function '), resto.find('\nfunction '))
              if c != -1]
    return js[i:i + len(firma) + (min(cortes) if cortes else len(resto))]


class TestLaPromesaRotaSeCumple:
    """Dos pantallas mandaban a un botón inexistente."""

    def test_las_pantallas_siguen_nombrando_el_boton(self):
        js = _js('compras_ia.js')
        assert js.count('Reconstruir stock diario') >= 2, (
            'si el aviso desapareció, este test protege algo que ya no existe')

    def test_y_ahora_el_boton_existe_con_ESE_nombre(self):
        """El nombre importa: quien lee el aviso busca esas palabras."""
        kardex = _js('kardex.js')
        assert 'Reconstruir stock diario' in kardex
        assert 'onclick="kardexReconstruir(false)"' in kardex

    def test_esta_en_el_panel_que_el_aviso_nombra(self):
        """El aviso dice «en Inventario › Datos». Ese panel es
        `inv-datos-container`, que renderiza `kardexCargarPanel`."""
        kardex = _js('kardex.js')
        cuerpo = _cuerpo(kardex, 'function _kardexRender(')
        assert 'kardexReconstruir' in cuerpo
        assert 'kardexReconciliar' in cuerpo


class TestElRechazoDelServidorNoSeEsconde:
    """`/reconstruir` es DENY-BY-DEFAULT y lo es por una razón cara.

    Reconstruir sobre un kardex truncado **fabrica días sin movimiento**: días
    que el descargador no trajo se leen como días sin venta, y de ahí como
    «agotado». Eso infla la corrección por censura y contamina el ROP y la
    temporada sin una sola alarma — justo el modelo que existe para corregir la
    censura, envenenado por una censura inventada.
    """

    def test_muestra_el_por_que_y_el_que_hacer_del_servidor(self):
        """El endpoint devuelve los dos campos. Mostrar solo «RECHAZADO» manda a
        la gente a buscar el override sin entender qué está saltando."""
        js = _js('kardex.js')
        cuerpo = _cuerpo(js, 'async function kardexReconstruir(')
        assert 'd.por_que' in cuerpo
        assert 'd.que_hacer' in cuerpo
        assert 'd.estado_descarga' in cuerpo

    def test_el_override_existe_y_no_esta_escondido(self):
        """Un rechazo que no se puede saltar se saltea por fuera del sistema."""
        js = _js('kardex.js')
        assert 'function kardexReconstruirForzar(' in js
        i = js.index('async function kardexReconstruir(')
        assert 'kardexReconstruirForzar()' in js[i:i + 3000]

    def test_el_override_confirma_nombrando_la_consecuencia(self):
        js = _js('kardex.js')
        cuerpo = _cuerpo(js, 'function kardexReconstruirForzar(')
        assert 'confirm(' in cuerpo
        assert 'inventa días sin movimiento' in cuerpo
        assert 'ROP' in cuerpo

    def test_lo_forzado_queda_declarado_en_el_resultado(self):
        """Si se forzó, el número resultante lleva su advertencia pegada. Un
        resultado forzado que se ve igual que uno limpio es el mismo problema
        una capa más abajo."""
        js = _js('kardex.js')
        cuerpo = _cuerpo(js, 'async function kardexReconstruir(')
        assert 'Se forzó sobre una descarga incompleta' in cuerpo

    def test_una_descarga_en_curso_no_se_confunde_con_un_rechazo(self):
        """409 es «ahora no», no «tus datos están mal». Mezclarlos haría que
        alguien fuerce el override por un problema de tiempo."""
        js = _js('kardex.js')
        cuerpo = _cuerpo(js, 'async function kardexReconstruir(')
        assert 'r.status === 409' in cuerpo


class TestLaCompuertaDeCompletitud:

    def test_distingue_abierta_de_cerrada(self):
        js = _js('kardex.js')
        cuerpo = _cuerpo(js, 'async function kardexReconciliar(')
        assert 'd.compuerta_ok === true' in cuerpo

    def test_nombra_los_conceptos_sin_clasificar(self):
        """«Hay 3 conceptos sin clasificar» no sirve: hay que saber CUÁLES para
        poder agregarlos a `CONCEPTO_DEFINICION`."""
        js = _js('kardex.js')
        cuerpo = _cuerpo(js, 'async function kardexReconciliar(')
        assert 'conceptos_detalle' in cuerpo
        assert 'v.clasificado' in cuerpo

    def test_muestra_el_cruce_con_su_instruccion(self):
        """El otro lado del cruce vive en Siesa: el sistema no lo puede cerrar
        solo, así que muestra su número y dice contra qué compararlo."""
        js = _js('kardex.js')
        cuerpo = _cuerpo(js, 'async function kardexReconciliar(')
        assert 'ventas_por_mes_bodega' in cuerpo
        assert 'd.instruccion' in cuerpo


class TestElSemaforoSobreLosModelos:
    """S-B y TSB leen del kardex y salen convincentes igual."""

    def test_existe_y_lo_llama_la_pantalla_de_modelos(self):
        js = _js('compras_ia.js')
        assert 'async function modelosSemaforoKardex(' in js
        i = js.index('async function modelosCargar(')
        assert 'modelosSemaforoKardex()' in js[i:i + 500]

    def test_tiene_donde_pintarse(self):
        """El div faltaba en flota y el botón respondía «no tengo dónde». Acá se
        verifica antes."""
        html = (_PWA / 'index.html').read_text(encoding='utf-8')
        assert 'id="modelos-semaforo"' in html

    def test_no_bloquea_el_modelo_que_la_persona_pidio(self):
        js = _js('compras_ia.js')
        i = js.index('async function modelosCargar(')
        assert 'await modelosSemaforoKardex' not in js[i:i + 500]

    def test_si_no_se_pudo_verificar_NO_dice_que_esta_bien(self):
        """Regla 0: el semáforo se apaga con una afirmación, no con la ausencia
        de una negación."""
        js = _js('compras_ia.js')
        cuerpo = _cuerpo(js, 'async function modelosSemaforoKardex(')
        assert 'No se pudo verificar' in cuerpo

    def test_manda_a_donde_se_arregla(self):
        """Un indicador rojo sin acción al lado se aprende a ignorar."""
        js = _js('compras_ia.js')
        cuerpo = _cuerpo(js, 'async function modelosSemaforoKardex(')
        assert 'Inventario › Datos' in cuerpo


class TestLaEvidenciaDeLaDemandaCorregida:
    """El «+18% por 62 días sin stock» que decidía y no se podía abrir.

    Es el mismo patrón que costó caro con el margen del armador: un número que
    ordena el contenedor y que nadie puede mirar. La diferencia entre un modelo
    y una superstición es poder abrir el dato.
    """

    def test_la_procedencia_es_lo_que_se_toca(self):
        js = _js('compras_ia.js')
        assert "repoVerEvidencia('${it.referencia}', 'rep-ev-${i}')" in js

    def test_hay_una_fila_donde_desplegarla(self):
        js = _js('compras_ia.js')
        assert '<tr id="rep-ev-${i}" data-abierto="0"></tr>' in js

    def test_esta_en_la_tabla_de_REPOSICION_nacional(self):
        """Hay dos tablas con la misma columna de procedencia; la evidencia de
        días sin stock corresponde a la de reposición, no a la de déficit."""
        js = _js('compras_ia.js')
        i = js.index('function _renderNacional(')
        fin = js.index('</tbody></table></div>', i)
        assert 'repoVerEvidencia' in js[i:fin]

    def test_agrupa_en_rachas_y_no_en_fechas_sueltas(self):
        """62 fechas sueltas no se leen; tres rachas sí. Un agotado importa por
        su duración, no por su cantidad de días."""
        js = _js('compras_ia.js')
        cuerpo = _cuerpo(js, 'async function repoVerEvidencia(')
        assert 'tramos' in cuerpo
        assert '86400000' in cuerpo, 'sin comparar días contiguos no hay rachas'

    def test_DECLARA_el_tope_de_365_filas(self):
        """LA TRAMPA DE ESTE ENDPOINT.

        `/stock-diario` devuelve máximo 365 FILAS, y cada fila es un
        día-bodega. Con cinco bodegas eso son ~73 días, no un año. Mostrar esa
        ventana recortada como si fuera completa haría que alguien concluya
        «estuvo agotado 3 días» sobre dos meses de historia, cuando el modelo
        usó doce.
        """
        js = _js('compras_ia.js')
        cuerpo = _cuerpo(js, 'async function repoVerEvidencia(')
        assert 'dias.length >= 365' in cuerpo, (
            'no se detecta el tope: la ventana recortada se vería completa')
        assert 'no cubre el año entero' in cuerpo

    def test_dice_que_bodegas_y_que_rango_cubre(self):
        """Sin el rango, el número de días agotados no se puede interpretar."""
        js = _js('compras_ia.js')
        cuerpo = _cuerpo(js, 'async function repoVerEvidencia(')
        assert 'bodegas' in cuerpo and 'fechas[0]' in cuerpo

    def test_un_SKU_sin_serie_reconstruida_lo_dice_y_manda_a_arreglarlo(self):
        """Es justo el SKU cuya demanda figura como CENSURADA. Mostrar «sin
        datos» a secas dejaría al usuario sin saber que eso tiene arreglo."""
        js = _js('compras_ia.js')
        cuerpo = _cuerpo(js, 'async function repoVerEvidencia(')
        assert 'CENSURADA' in cuerpo
        assert 'Reconstruir stock diario' in cuerpo


class TestElContratoDelServidorSigueSiendoElQueLaPantallaLee:
    """Los de arriba miran el cliente. Estos, que el servidor no haya cambiado.

    Una pantalla que lee una clave que dejó de existir se queda en blanco sin
    error — el modo de fallo que ya costó dos veces en este proyecto.
    """

    def test_reconciliar_trae_las_claves_del_semaforo(self, client, jwt_token_admin):
        """De verdad, por HTTP, contra la base de los tests.

        Hasta el 2026-08-06 esto era imposible: `reconciliar_kardex` agrupaba
        con `func.to_char`, que es de PostgreSQL, y en SQLite devolvía 500. El
        endpoint funcionaba **solo en producción** y ningún test lo tocaba —
        justo el que ahora alimenta el semáforo de confianza de la pantalla
        Modelos.

        Se pasó a `extract('year'|'month')`, que es SQL estándar. Verificado a
        mano contra los dos motores con las mismas filas: mismo resultado.
        """
        r = client.get('/api/kardex/reconciliar?meses=12',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        for k in ('compuerta_ok', 'conceptos_detalle', 'conceptos_desconocidos',
                  'total_registros_kardex', 'bodegas_con_datos',
                  'ventas_por_mes_bodega', 'instruccion'):
            assert k in d, f'reconciliar dejó de devolver {k}'

    def test_el_mes_sale_en_formato_YYYY_MM(self, app, db, client, jwt_token_admin):
        """El formato lo compone Python ahora. Un mes de un dígito sin rellenar
        —`2026-3`— rompe el orden alfabético de la tabla y nadie lo nota hasta
        que marzo aparece después de noviembre."""
        from datetime import date

        from app.services.kardex_service import KardexMovimiento

        db.session.add(KardexMovimiento(
            referencia='KF1', bodega='NB1', fecha=date(2026, 3, 5),
            concepto=501, naturaleza=2, cantidad=10, tipo_docto='XX'))
        db.session.commit()

        d = client.get('/api/kardex/reconciliar?meses=120',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'}).get_json()
        meses = [v['mes'] for v in d['ventas_por_mes_bodega']]
        assert '2026-03' in meses, meses

    def test_ningun_endpoint_del_kardex_usa_to_char(self):
        """TRINQUETE — `to_char` vuelve el endpoint no-testeable sin avisar.

        No falla al escribirlo: falla en la suite con un 500 que parece un bug
        de otra cosa, o directamente nunca se prueba porque nadie escribió el
        test.

        Por AST y no por texto: la primera versión buscaba la cadena y se
        atrapó en el comentario que explica por qué NO se usa. Lo que hace
        ilegal a la llamada es que sea una llamada.
        """
        import ast
        from pathlib import Path as _P

        fuente = (_P(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'kardex_service.py').read_text(encoding='utf-8')
        fn = next(n for n in ast.walk(ast.parse(fuente))
                  if isinstance(n, ast.FunctionDef) and n.name == 'reconciliar_kardex')
        malas = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'to_char']
        assert not malas, (
            f'reconciliar_kardex volvió a llamar to_char (línea {malas}): solo '
            'corre en PostgreSQL y la suite deja de poder ejercerlo')

    def test_stock_diario_trae_las_claves_de_la_evidencia(self, client, jwt_token_admin):
        r = client.get('/api/kardex/stock-diario?referencia=NO-EXISTE',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        d = r.get_json()
        assert 'dias' in d and isinstance(d['dias'], list)

    def test_stock_diario_sin_referencia_es_400(self, client, jwt_token_admin):
        r = client.get('/api/kardex/stock-diario',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 400

    def test_el_tope_de_365_sigue_ahi(self):
        """El aviso de truncamiento de la pantalla depende de este número. Si el
        endpoint sube el límite y la pantalla no, el aviso pasa a mentir."""
        fuente = (Path(__file__).resolve().parents[1] / 'app' / 'routes'
                  / 'kardex.py').read_text(encoding='utf-8')
        i = fuente.index('def stock_diario(')
        assert '.limit(365)' in fuente[i:i + 1200], (
            'cambió el tope del endpoint: actualizar también el aviso de '
            'truncamiento en `repoVerEvidencia`, que hoy compara contra 365')

    def test_reconstruir_rechaza_sin_descarga_completa(self, client, jwt_token_admin):
        """El deny-by-default que la pantalla muestra. Si el servidor dejara de
        rechazar, la pantalla mostraría un camino que ya no existe."""
        r = client.post('/api/kardex/reconstruir', json={},
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        # 409, el MISMO código que «hay una descarga en curso». Por eso la
        # pantalla los separa por el cuerpo y no por el status: distinguirlos
        # mal mostraba «esperá a que termine» sobre un problema que no se
        # arregla esperando, y escondía el override.
        assert r.status_code == 409
        d = r.get_json()
        assert 'por_que' in d and 'que_hacer' in d and 'override' in d

    def test_los_tres_son_solo_de_gestion(self, client, jwt_token):
        for metodo, ruta in (('GET', '/api/kardex/reconciliar'),
                             ('GET', '/api/kardex/stock-diario?referencia=X'),
                             ('POST', '/api/kardex/reconstruir')):
            r = client.open(ruta, method=metodo,
                            headers={'Authorization': f'Bearer {jwt_token}'})
            assert r.status_code == 403, f'{ruta} no exige rol de gestión'
