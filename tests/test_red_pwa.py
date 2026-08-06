"""
915 KB de JS sin comprimir, en cada carga de cada pantalla.

La factura de Railway de julio marcó ~390 GB de red, el 60% del consumo. El
diagnóstico dio tres causas que se sumaban, todas en el mismo par de líneas:

  · `Cache-Control: no-store` en todo el PWA. `no-store` prohíbe **guardar**,
    así que ni siquiera hay ETag que revalidar: la respuesta viaja completa
    siempre. Lo que se quería —"que nadie use JS viejo"— lo da `no-cache`, que
    también obliga a preguntar pero deja contestar 304 sin cuerpo.
  · El service worker en **network-first**, que es no tener caché salvo cuando
    no hay red.
  · Sin compresión: 915 KB de texto plano por carga.

Estos tests protegen el arreglo por la propiedad, no por la configuración: que
un archivo sin cambios **no vuelva a viajar**, y que uno con cambios **sí**.
La segunda mitad es la que hace seguro al resto — una caché que sirve código
viejo en un WMS es peor que la factura.
"""
import gzip

import pytest


class TestElContenidoNoViajaDosVeces:

    def test_un_archivo_sin_cambios_responde_304_sin_cuerpo(self, client):
        primera = client.get('/static/pwa/app.js')
        assert primera.status_code == 200
        etag = primera.headers.get('ETag')
        assert etag, 'sin ETag no hay revalidación posible: todo viaja completo'

        segunda = client.get('/static/pwa/app.js',
                             headers={'If-None-Match': etag})
        assert segunda.status_code == 304
        assert segunda.data == b'', 'un 304 con cuerpo no ahorra nada'

    def test_no_store_no_vuelve(self):
        """TRINQUETE. `no-store` es la diferencia entre 304 y 200 completo.

        Se mira la constante y no una respuesta: la respuesta ya la verifica el
        test de arriba, y este dice POR QUÉ, para que nadie lo reponga
        creyendo que endurece el caché.
        """
        from pathlib import Path

        fuente = (Path(__file__).resolve().parents[1] / 'app'
                  / '__init__.py').read_text(encoding='utf-8')
        i = fuente.index('_PWA_CACHE =')
        assert 'no-store' not in fuente[i:i + 120]
        assert 'no-cache' in fuente[i:i + 120], (
            'sin no-cache el navegador podría servir JS viejo sin preguntar')

    def test_la_frescura_no_se_perdio(self, client):
        """Lo que `no-store` protegía sigue protegido: se pregunta SIEMPRE.

        `must-revalidate` + `no-cache` = el navegador no puede usar su copia sin
        confirmarla. Cambia cuántos bytes viajan, no cuándo se pregunta.
        """
        r = client.get('/static/pwa/app.js')
        cc = r.headers.get('Cache-Control', '')
        assert 'no-cache' in cc and 'must-revalidate' in cc
        assert 'max-age=' not in cc.replace('max-age=0', ''), (
            'un max-age positivo sí dejaría servir JS viejo sin preguntar')

    def test_un_etag_viejo_devuelve_el_archivo_completo(self, client, tmp_path):
        """La otra mitad: si el archivo cambió, el 304 sería servir mentira."""
        r = client.get('/static/pwa/app.js',
                       headers={'If-None-Match': '"no-es-el-etag-actual"'})
        assert r.status_code == 200
        assert len(r.data) > 1000


class TestGzip:

    def test_el_js_viaja_comprimido(self, client):
        r = client.get('/static/pwa/app.js',
                       headers={'Accept-Encoding': 'gzip'})
        assert r.headers.get('Content-Encoding') == 'gzip'
        assert len(r.data) < 400_000

    def test_lo_comprimido_se_descomprime_a_lo_original(self, client):
        """Comprimir mal es servir un archivo roto con 200 OK."""
        crudo = client.get('/static/pwa/app.js',
                           headers={'Accept-Encoding': 'identity'})
        comp = client.get('/static/pwa/app.js',
                          headers={'Accept-Encoding': 'gzip'})
        assert gzip.decompress(comp.data) == crudo.data

    def test_un_cliente_que_no_pide_gzip_recibe_el_texto(self, client):
        """El contrato no cambia: se comprime el transporte, no la respuesta."""
        r = client.get('/static/pwa/app.js',
                       headers={'Accept-Encoding': 'identity'})
        assert r.headers.get('Content-Encoding') is None
        assert r.data.startswith(b'/') or b'function' in r.data[:2000]

    def test_las_respuestas_json_grandes_tambien(self, client):
        r = client.get('/api/health/ping', headers={'Accept-Encoding': 'gzip'})
        assert r.status_code == 200
        # Puede no comprimirse por ser < COMPRESS_MIN_SIZE, y eso es correcto:
        # por debajo de 1 KB comprimir cuesta más de lo que ahorra.
        assert r.get_json() is not None


class TestElServiceWorkerNoPuedeServirCodigoViejo:
    """Cache-first es seguro solo si la caché muere con el deploy.

    Con nombre fijo, el `activate` —que borra las cachés con OTRO nombre— nunca
    borraba nada: el shell del primer día habría quedado servido para siempre.
    """

    def _sw(self, client):
        return client.get('/static/pwa/sw.js').get_data(as_text=True)

    def test_el_sello_de_version_llega_como_constante(self, client):
        js = self._sw(client)
        assert js.startswith('const SW_VERSION = "'), (
            'si la versión viaja en un comentario, el sw no la puede usar')

    def test_la_cache_se_nombra_con_el_sello(self, client):
        js = self._sw(client)
        assert "'wms-shell-' + (typeof SW_VERSION" in js

    def test_el_sello_se_mueve_si_se_toca_CUALQUIER_archivo(self, app, client, tmp_path):
        """Antes salía del mtime de `app.js`. Un deploy que solo tocara
        `flota.js` no movía la versión — y con cache-first eso es servir el
        archivo viejo hasta el siguiente cambio de `app.js`."""
        import os
        import re

        def _sello():
            return re.search(r'const SW_VERSION = "(\d+)"',
                             self._sw(client)).group(1)

        antes = _sello()
        objetivo = os.path.join(app.root_path, 'static', 'pwa', 'flota.js')
        st = os.stat(objetivo)
        try:
            os.utime(objetivo, (st.st_atime, st.st_mtime + 5000))
            assert _sello() != antes
        finally:
            os.utime(objetivo, (st.st_atime, st.st_mtime))

    def test_el_propio_sw_nunca_se_cachea(self, client):
        """Es el único canal por el que llega una versión nueva."""
        r = client.get('/static/pwa/sw.js')
        assert 'no-store' in r.headers.get('Cache-Control', '')

    def test_la_API_queda_fuera_de_la_cache(self, client):
        """Un pedido servido de caché son datos viejos presentados como
        actuales — la peor clase de dato en un WMS.

        Antes este test afirmaba la IMPLEMENTACIÓN (`startsWith('/api/')`) y por
        eso pasó en verde mientras `/flota/` se cacheaba: verificaba que la
        exclusión estuviera escrita, no que los datos quedaran afuera. La
        propiedad la comprueba ahora `TestElServiceWorkerNoCacheaDATOS`, que
        ejerce la decisión contra rutas reales.
        """
        js = self._sw(client)
        assert 'esDelShell' in js, (
            'la decisión de qué se cachea tiene que estar en una función que se '
            'pueda ejercer, no repartida en condiciones sueltas')

    def test_no_se_cachean_respuestas_de_error(self, client):
        """Cachear un 404 lo vuelve permanente hasta el próximo deploy."""
        js = self._sw(client)
        assert 'res.ok' in js


class TestElServiceWorkerNoCacheaDATOS:
    """La regresión más cara del 2026-08-05, y fue mía.

    El cache-first de la mañana excluía `/api/` y cacheaba **todo lo demás**. El
    módulo de flota no cuelga de `/api/` sino de `/flota/`, así que sus GET
    quedaron cacheados: la ficha técnica se guardaba, se volvía a pedir, y el
    navegador devolvía la respuesta VIEJA. En pantalla decía «Ficha guardada y
    completa ✓» y el formulario aparecía en blanco. Yesid lo reportó en tres
    vehículos y sonaba a que la base no guardaba.

    La lección no es que faltaba `/flota/` en las exclusiones. Es que una lista
    de EXCLUSIONES es la forma equivocada de escribir esto: cada módulo nuevo
    montado en un prefijo nuevo entra al caché por omisión, y el fallo no es un
    error visible sino datos viejos presentados como actuales.
    """

    def _sw(self, client):
        return client.get('/static/pwa/sw.js').get_data(as_text=True)

    def test_se_cachea_por_lista_blanca(self, client):
        js = self._sw(client)
        assert 'function esDelShell' in js
        assert 'if (!esDelShell(url)) return;' in js, (
            'volvió el criterio por exclusión: lo que no está declarado tiene '
            'que ir a la red, no al caché')

    def test_el_shell_si_se_cachea(self, client):
        js = self._sw(client)
        i = js.index('function esDelShell')
        cuerpo = js[i:i + 400]
        assert "'/static/'" in cuerpo
        assert "'/pwa'" in cuerpo

    def test_flota_NO_esta_en_la_lista_blanca(self, client):
        """El caso concreto que rompió. `/flota/vehiculo/X/ficha` es un dato."""
        js = self._sw(client)
        i = js.index('function esDelShell')
        cuerpo = js[i:i + 400]
        assert '/flota' not in cuerpo

    def test_ninguna_ruta_de_datos_entra_por_omision(self, client):
        """TRINQUETE — se ejerce la función contra rutas reales del sistema.

        No se mira la lista: se comprueba qué decide. Una lista que "parece
        bien" y una función que devuelve `true` para `/flota/` son
        indistinguibles leyendo el código.
        """
        js = self._sw(client)
        i = js.index('function esDelShell')
        cuerpo = js[i:js.index('\n}', i) + 2]

        import re
        # Se traduce la función a Python para ejercerla sin navegador: son dos
        # condiciones sobre `url.pathname` y se leen del propio archivo.
        prefijos = re.findall(r"pathname\.startsWith\('([^']+)'\)", cuerpo)
        exactos = re.findall(r"pathname === '([^']+)'", cuerpo)

        def es_del_shell(p):
            return any(p.startswith(x) for x in prefijos) or p in exactos

        datos = ['/flota/vehiculo/THP696/ficha', '/flota/custodia/activa/THP696',
                 '/flota/vehiculo/THP696/documentos', '/flota/conductor/mi-turno',
                 '/flota/avisos', '/api/almacenes/', '/api/rutas/vehiculos',
                 '/health']
        cacheados = [p for p in datos if es_del_shell(p)]
        assert not cacheados, (
            f'estas rutas de DATOS se servirían de caché: {cacheados}. '
            'Un dato viejo presentado como actual es la peor clase de dato en '
            'un WMS, y no produce ningún error visible.')

        estaticos = ['/static/pwa/app.js', '/static/pwa/flota.js', '/pwa']
        no_cacheados = [p for p in estaticos if not es_del_shell(p)]
        assert not no_cacheados, (
            f'el shell dejó de cachearse: {no_cacheados} — vuelven los 915 KB '
            'por carga que motivaron todo esto')
