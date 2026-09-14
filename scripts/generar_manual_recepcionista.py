# -*- coding: utf-8 -*-
"""Genera app/static/manuales/manual_recepcionista_nb1.pdf, replicando el
mismo estilo visual y estructura de manual_picker_nb1.pdf / manual_empacador_nb1.pdf
(cover con Papelería Medellín + título teal + subtítulo + versión; secciones
numeradas; tabla Sí/No; notas en cursiva "Tenga en cuenta"; preguntas
frecuentes; pie de página citando los archivos fuente).

Requiere reportlab (no es dependencia del runtime de la app -- solo de
generación): venv/Scripts/pip install reportlab

Uso:
    venv/Scripts/python.exe scripts/generar_manual_recepcionista.py
"""
import os
import re
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEAL = colors.HexColor('#1E8395')
BLUEGRAY = colors.HexColor('#44607A')
BLACK = colors.HexColor('#1A1A1A')
LINE = colors.HexColor('#C9D3D8')
ROWALT = colors.HexColor('#EEF3F5')

OUT = os.path.join(REPO_ROOT, 'app', 'static', 'manuales', 'manual_recepcionista_nb1.pdf')

styles = getSampleStyleSheet()

brand = ParagraphStyle('brand', fontName='Helvetica', fontSize=13, leading=16,
                        textColor=BLUEGRAY, alignment=TA_CENTER, spaceAfter=6)
maintitle = ParagraphStyle('maintitle', fontName='Helvetica-Bold', fontSize=29, leading=34,
                            textColor=TEAL, alignment=TA_CENTER, spaceBefore=4, spaceAfter=16)
subtitle = ParagraphStyle('subtitle', fontName='Helvetica', fontSize=14.5, leading=19,
                           textColor=BLUEGRAY, alignment=TA_CENTER, spaceAfter=10)
version = ParagraphStyle('version', fontName='Helvetica-Oblique', fontSize=10.5, leading=14,
                          textColor=BLACK, alignment=TA_CENTER)

h2 = ParagraphStyle('h2', fontName='Helvetica-Bold', fontSize=14, leading=18,
                     textColor=TEAL, spaceBefore=16, spaceAfter=8)
body = ParagraphStyle('body', fontName='Helvetica', fontSize=10.5, leading=15.5,
                       textColor=BLACK, alignment=TA_LEFT, spaceAfter=8)
note = ParagraphStyle('note', parent=body, fontName='Helvetica-Oblique', textColor=BLUEGRAY)
bullet = ParagraphStyle('bullet', parent=body, leftIndent=16, bulletIndent=4, spaceAfter=4)
numitem = ParagraphStyle('numitem', parent=body, leftIndent=18, spaceAfter=4)
faqq = ParagraphStyle('faqq', parent=body, fontName='Helvetica-Bold', spaceBefore=8, spaceAfter=2)
faqa = ParagraphStyle('faqa', parent=body, spaceAfter=2)
footer = ParagraphStyle('footer', parent=body, fontName='Helvetica-Oblique', fontSize=9.5,
                         textColor=BLUEGRAY)

# Helvetica (fuente base de PDF, sin fuente embebida) no trae glifos de
# emoji/pictogramas -- quedan como hueco en blanco. Se retiran junto con el
# espacio que los sigue (si lo hay) para no dejar "  Escanear" con doble
# espacio donde iba el ícono. Ojo al agregar texto nuevo: cualquier símbolo
# fuera de WinAnsi (flechas →, signo menos real −, etc.) también rompe el
# render -- usar "-", ":" o palabras en su lugar.
_EMOJI_RE = re.compile(
    '[\U0001F300-\U0001FAFF☀-➿️]\\s?'
)

def _sanitize(txt):
    return re.sub(r' {2,}', ' ', _EMOJI_RE.sub('', txt)).strip()

def P(txt, style=body):
    return Paragraph(_sanitize(txt), style)

def bullets(items, style=bullet):
    return [P('•  ' + t, style) for t in items]

def numbered(items, style=numitem, start=1):
    out = []
    for i, t in enumerate(items, start=start):
        out.append(P(f'{i}. {t}', style))
    return out

_cell = ParagraphStyle('cell', parent=body, fontSize=9.6, leading=13, spaceAfter=0)
_cellhead = ParagraphStyle('cellhead', parent=_cell, fontName='Helvetica-Bold', textColor=TEAL)

def sino_table(rows, col_widths=(2.55*inch, 2.55*inch)):
    header = [Paragraph(_sanitize('Usted SÍ puede'), _cellhead),
               Paragraph(_sanitize('Usted NO puede'), _cellhead)]
    data = [header] + [[Paragraph(_sanitize(c), _cell) if c else '' for c in row] for row in rows]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#D9E6EA')),
        ('TEXTCOLOR', (0, 0), (-1, 0), TEAL),
        ('GRID', (0, 0), (-1, -1), 0.6, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, ROWALT]),
    ]))
    return t

_pregh = ParagraphStyle('pregh', parent=_cellhead, fontSize=8.8, leading=11.5)

def pregunta_table(rows):
    """rows: [(pregunta, por_que, que_hacer), ...]"""
    data = [[Paragraph(_sanitize(h), _pregh) for h in
             ('Cuando el sistema le pregunta...', 'Por qué', 'Qué hacer')]]
    for q, w, d in rows:
        data.append([P(q, ParagraphStyle('c1', parent=body, fontName='Helvetica-Bold', fontSize=9.4)),
                      P(w, ParagraphStyle('c2', parent=body, fontSize=9.2)),
                      P(d, ParagraphStyle('c3', parent=body, fontSize=9.2))])
    t = Table(data, colWidths=(1.7*inch, 1.9*inch, 2.0*inch))
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#D9E6EA')),
        ('TEXTCOLOR', (0, 0), (-1, 0), TEAL),
        ('GRID', (0, 0), (-1, -1), 0.6, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, ROWALT]),
    ]))
    return t

story = []

# ---------- Portada ----------
story += [Spacer(1, 1.7*inch),
          P('Papelería Medellín', brand),
          P('Manual del Recepcionista', maintitle),
          P('Guía para su trabajo diario en el WMS — Bodega NB1', subtitle),
          P('Versión 1.0', version),
          PageBreak()]

# ---------- 1. Bienvenido ----------
story += [P('1. Bienvenido', h2),
    P('Este manual es para usted, que trabaja como Recepcionista en la bodega NB1. Aquí '
      'encuentra, paso a paso, todo lo que necesita saber para usar la aplicación del WMS en '
      'su turno de trabajo.', body),
    P('El WMS es la aplicación donde usted recibe la mercancía que entra a la bodega: pedidos '
      'de proveedores (órdenes de compra) y traslados que llegan desde NB1. Usted la usa desde '
      'el celular o la tablet que le entrega la bodega.', body),
]

# ---------- 2. Cuál es su rol ----------
story += [P('2. Cuál es su rol', h2),
    P('Usted es la persona que cuenta físicamente lo que llega —de un proveedor o de otra '
      'bodega—, lo compara contra lo que el sistema espera, y confirma la entrada. El sistema '
      'actualiza el inventario automáticamente y le avisa a Siesa.', body),
    P('Su pantalla tiene tres pestañas: <b>OCs</b> (pedidos a proveedores), <b>Traslados</b> '
      '(mercancía que llega de NB1) y <b>Devoluciones</b> (clientes que devuelven mercancía). '
      'Este manual cubre las dos primeras — Devoluciones tiene su propio manual aparte.', body),
]

# ---------- 3. Qué puede y qué no puede hacer ----------
story += [P('3. Qué puede y qué no puede hacer', h2),
    P('Usted NO tiene acceso al panel administrativo del WMS (Dashboard, Pedidos, Rutas, '
      'Usuarios, Liquidación, Siesa, Compras, etc.). Esas pantallas son solo para los roles de '
      'gestión (administrador, supervisor, jefe de almacén, gerente).', body),
    Spacer(1, 4),
    sino_table([
        ['Ver las OC pendientes, en proceso y recepcionadas', 'Crear o anular una OC — eso se hace en Siesa'],
        ['Iniciar o continuar la recepción de una OC', 'Confirmar una recepción sin el número de remisión del proveedor'],
        ['Escanear y contar cada producto que llega', 'Ver su propia productividad dentro de la aplicación'],
        ['Registrar obsequios o bonificaciones del proveedor', 'Editar cantidades de una OC ya recepcionada'],
        ['Ver y contar los traslados pendientes desde NB1', ''],
        ['Guardar una recepción a medias y continuarla después', ''],
    ]),
]

# ---------- 4. Cómo ingresar ----------
story += [P('4. Cómo ingresar a la aplicación', h2)]
story += numbered([
    'Abra la aplicación del WMS desde el navegador de su celular o tablet.',
    'Escriba su correo y su contraseña.',
    'Pulse "Entrar al sistema".',
])
story += [P('La aplicación reconoce su rol automáticamente y lo lleva directo a su pantalla de '
             'Recepción — usted no tiene que elegir ningún menú.', body)]

# ---------- 5. Su pantalla de trabajo ----------
story += [P('5. Su pantalla de trabajo', h2),
    P('Al entrar ve tres pestañas arriba: <b>OCs</b>, <b>Traslados</b> y <b>Devoluciones</b>. '
      'Dentro de OCs y Traslados, las tareas se organizan en listas separadas según en qué '
      'etapa están — pendiente, en proceso o ya recepcionada.', body),
]

# ---------- 6. OC ----------
story += [PageBreak(), P('6. Cómo recibir una orden de compra (OC), paso a paso', h2),
    P('La pestaña OCs tiene tres listas: <b>Pendientes en Siesa</b> (todavía no ha empezado), '
      '<b>En proceso</b> (ya empezó a contar) y <b>Recepcionadas</b> (ya confirmadas).', body),
    P('Si ve el aviso <i>"⚠ Siesa no respondió"</i>, espere un momento y refresque — el sistema '
      'no puede traer la lista de OCs sin conexión con Siesa.', note),
]
story += numbered([
    'OC nueva: pulse "Iniciar recepción". OC que ya empezó: pulse "Continuar recepción"; '
    'retoma exactamente donde quedó.',
    'Cuente la mercancía siguiendo la guía en pantalla: <i>"Escanea unidad, caja o paca — el '
    'sistema calcula las unidades"</i>. Use "📷 Escanear con cámara", o escriba el código a mano. '
    'Si el producto no tiene código de barras, use "📦 Sin código — buscar producto manualmente".',
    'Cada línea muestra una barra recibido/ordenado que se pone verde al completarse.',
])
story += [P('Tenga en cuenta: puede que el sistema le pregunte algo antes de dejarlo avanzar. '
             'No son errores — son decisiones que solo usted puede tomar mirando la mercancía '
             'física. Vea la tabla a continuación.', note),
]

# ---------- 7. Preguntas del sistema ----------
story += [P('7. Las cuatro preguntas que le puede hacer el sistema', h2)]
story += [pregunta_table([
    ('¿Unidad o caja?',
     'El código escaneado no distingue si es 1 unidad o una caja completa.',
     'Mire la mercancía y elija lo que tiene en la mano.'),
    ('"⚠️ Código ambiguo"',
     'El código GS1 puede corresponder a más de un tipo de empaque.',
     'Elija el empaque real que está contando.'),
    ('"Producto fuera de OC — ¿es obsequio?"',
     'Llegó algo que el pedido no tenía.',
     'Si de verdad es un regalo del proveedor, responda "Sí" (o use "🎁 Registrar Obsequio/'
     'Bonificación" directo).'),
    ('Paca sin ningún código',
     'El sistema no tiene forma de saber cuánto trae esa paca.',
     'Escriba la cantidad — genera una etiqueta nueva (LPN); imprímala y péguela a la paca.'),
])]

# ---------- 8. Confirmar OC ----------
story += [P('8. Cómo confirmar la recepción de una OC', h2)]
story += numbered([
    'Si falta algo por contar, el sistema le pregunta: "¿Confirmar como recepción parcial?"',
    'Siempre le va a pedir el <b>número de remisión del proveedor</b> antes de cerrar — Siesa '
    'lo necesita para procesar la entrada. Téngalo a mano desde que arranca.',
    'Al confirmar verá "Recepción confirmada — Siesa actualizó inventario".',
])
story += [P('¿No va a terminar ahora? Pulse "Guardar y salir (continuar más tarde)" — lo que ya '
             'contó queda guardado, nadie lo pierde. Retómelo después desde "En proceso".', note)]

# ---------- 9. Traslados ----------
story += [P('9. Cómo recibir un traslado desde NB1, paso a paso', h2),
    P('Cada tarjeta de la pestaña Traslados muestra el código de la solicitud (ST-...), desde '
      'qué bodega viene (normalmente NB1), y cuántos ítems/unidades esperadas trae.', body),
]
story += numbered([
    'Pulse "📋 Contar productos" sobre la tarjeta que le corresponde.',
    'Escanee cada producto, o ajuste a mano con los botones "-" / "+". El sistema no le deja '
    'pasarse de la cantidad esperada.',
    'Si escanea algo que no pertenece a ese traslado, avisa: "Código no encontrado en este '
    'traslado".',
    'Confirme. Si falta algo, igual que en OC, le pregunta si es parcial.',
])
story += [
    P('Al confirmar verá "✓ Recepción confirmada — ETS generado en Siesa" — significa que '
      'Siesa ya registró la entrada.', body),
    P('¿Se equivocó antes de confirmar? Pulse "Cancelar — volver a la lista", todavía no pasó '
      'nada.', note),
]

# ---------- 10. Lo que nunca debe hacer ----------
story += [P('10. Lo que nunca debe hacer', h2)]
story += bullets([
    'No confirme una OC o traslado sin contar físicamente lo que llegó.',
    'No invente el número de remisión — sin el real, Siesa puede rechazar la entrada.',
    'No marque algo como "obsequio" solo para que el sistema lo deje avanzar.',
    'No cierre la aplicación a la mitad de un conteo — use "Guardar y salir" si necesita parar.',
])

# ---------- 11. Al terminar su turno ----------
story += [P('11. Al terminar su turno', h2),
    P('Para cerrar su sesión, pulse "Salir". Esto borra la sesión de su dispositivo.', body),
    P('Tenga en cuenta: usted no tiene una pantalla propia donde ver cuánto recepcionó en el '
      'día — esa información la consulta su jefe de almacén o el administrador desde el '
      'Dashboard.', note),
]

# ---------- 12. FAQ ----------
story += [PageBreak(), P('12. Preguntas frecuentes', h2)]
faq = [
    ('¿Puedo recibir una OC que no está en la lista de Siesa?',
     'No. Si la OC no aparece, avise a compras — puede que todavía no esté aprobada o '
     'registrada en Siesa.'),
    ('¿Qué pasa si escaneo un código que no está en la OC ni es un obsequio?',
     'La aplicación no lo deja avanzar con ese ítem — verifique que está escaneando el '
     'producto correcto antes de marcarlo como obsequio.'),
    ('¿Puedo dejar una recepción a medias y seguir con otra?',
     'Sí. Use "Guardar y salir (continuar más tarde)" — lo que contó queda guardado y puede '
     'retomarlo cuando quiera desde "En proceso".'),
    ('¿Qué hago si no tengo el número de remisión del proveedor?',
     'Consiga el remito físico del transportador antes de confirmar — el sistema no cierra la '
     'recepción sin ese número.'),
    ('¿Puedo ver mi productividad del día?',
     'No. Esa información solo la ven los roles de gestión (jefe de almacén, supervisor, '
     'administrador, gerente).'),
]
for q, a in faq:
    story += [P(q, faqq), P(a, faqa)]

# ---------- Pie ----------
story += [Spacer(1, 26),
    P('Documento elaborado a partir del código real de la aplicación '
      '(app/static/pwa/recepcion.js, app/routes/recepcion.py, app/routes/traslados.py, '
      'app/routes/_auth_helpers.py) — describe lo que la aplicación hace hoy en producción, no '
      'funcionalidad planeada.', footer),
]

doc = SimpleDocTemplate(OUT, pagesize=LETTER,
                         topMargin=0.9*inch, bottomMargin=0.9*inch,
                         leftMargin=0.95*inch, rightMargin=0.95*inch,
                         title='Manual del Recepcionista', author='Papelería Medellín')
doc.build(story)
print('Generado:', OUT, '-', os.path.getsize(OUT), 'bytes')
