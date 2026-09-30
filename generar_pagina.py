"""
Puente — generador de página (v2: categorías, buscador, palabra del día,
comparación de medios, página de detalle por historia)

Lee las historias ya agrupadas (tabla `historias` en noticias.db, armada
por agrupar.py) y genera:
  - docs/index.html: historias con cobertura de 3+ medios, con filtros
    por categoría y buscador (client-side), palabra del día e indicador
    de inclinación política.
  - docs/historias/<id>.html: una página de detalle por cada historia
    que se muestra en la home.
  - docs/comparacion.html: matriz de solapamiento de cobertura entre
    todos los medios confirmados.
  - docs/tendencias.html: temas en tendencia de búsqueda en Argentina
    (Google Trends) cruzados con nuestras historias — los datos los arma
    tendencias.py, este script solo los lee de noticias.db y los dibuja.

Requiere haber corrido antes recolector.py, agrupar.py y (para la pestaña
de tendencias) tendencias.py — si tendencias.py nunca corrió, la pestaña
se genera vacía en vez de romper el resto del sitio.

Uso:
    python generar_pagina.py
"""

import json
from html import unescape as desescapar_html
import os
import re
import sqlite3
import statistics
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import yaml
from jinja2 import Environment, FileSystemLoader

DB_PATH = "noticias.db"
FEEDS_PATH = "feeds.yaml"
# docs/ es lo que GitHub Pages sirve como sitio
DOCS_DIR = "docs"
SALIDA = os.path.join(DOCS_DIR, "index.html")
SALIDA_COMPARACION = os.path.join(DOCS_DIR, "comparacion.html")
SALIDA_NOSOTROS = os.path.join(DOCS_DIR, "nosotros.html")
SALIDA_GUARDADAS = os.path.join(DOCS_DIR, "guardadas.html")
SALIDA_TENDENCIAS = os.path.join(DOCS_DIR, "tendencias.html")
SALIDA_DATOS_COMPARACION = os.path.join(DOCS_DIR, "datos_comparacion.json")
DIR_HISTORIAS = os.path.join(DOCS_DIR, "historias")

MEDIOS_MINIMOS = 3  # una historia solo entra a la página si la cubrieron al menos estos medios distintos

# Escala de posición editorial: 1=Izquierda .. 21=Derecha, centro en 11
ESCALA_MIN = 1
ESCALA_MAX = 21
ESCALA_CENTRO = 11

ETIQUETAS_POSICION = {
    1: "Izquierda",
    2: "Centro-izquierda",
    3: "Centro",
    4: "Centro-derecha",
    5: "Derecha",
}
ORDEN_BUCKETS = [ETIQUETAS_POSICION[i] for i in (1, 2, 3, 4, 5)]

# --- Categorías (clasificación por palabras clave, sin LLM) ---------------

CATEGORIAS = ["Política nacional", "Geopolítica", "Economía", "Deportes", "Asuntos internos"]

PALABRAS_CATEGORIA = {
    "Política nacional": [
        "milei", "gobierno", "congreso", "senado", "diputados", "gabinete",
        "ministro", "ministra", "casa rosada", "decreto", "proyecto de ley",
        "kirchnerismo", "peronismo", "libertad avanza", "oposicion",
        "gobernador", "gobernadora", "provincia de buenos aires",
        "elecciones", "candidato", "candidata", "votos", "boleta",
        "paso", "camara de diputados", "camara de senadores", "bloque",
        "presidente", "vicepresidente", "justicia electoral",
    ],
    "Geopolítica": [
        "onu", "trump", "guerra", "israel", "gaza", "ucrania", "rusia",
        "china", "estados unidos", "embajada", "embajador", "cumbre",
        "casa blanca", "putin", "otan", "medio oriente", "iran",
        "union europea", "palestina", "franja de gaza", "zelenski",
        "misiles", "conflicto armado", "naciones unidas",
    ],
    "Economía": [
        "dolar", "inflacion", "mercado", "bcra", "fmi", "tasas",
        "riesgo pais", "bonos", "exportaciones", "importaciones", "pbi",
        "recesion", "salario", "salarios", "precios", "impuesto",
        "impuestos", "tarifas", "reservas", "deuda externa", "superavit",
        "actividad economica", "aranceles", "banco central", "acciones",
        "bolsa", "canasta basica",
    ],
    "Deportes": [
        "futbol", "boca", "river", "seleccion", "messi", " gol ", "goles",
        "liga profesional", "champions", "mundial", "tenis", "basquet",
        "racing", "independiente", "san lorenzo", "copa libertadores",
        "copa argentina", "afa", "estadio", "partido de",
    ],
}


def _normalizar(txt: str) -> str:
    txt = txt.lower()
    txt = unicodedata.normalize("NFKD", txt)
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return f" {txt} "


def categoria_de_historia(titulo: str) -> str:
    t = _normalizar(titulo)
    for cat, palabras in PALABRAS_CATEGORIA.items():
        for p in palabras:
            if _normalizar(p).strip() in t:
                return cat
    return "Asuntos internos"


# --- Palabra del día -------------------------------------------------------

STOPWORDS = set("""
que los las del para con una uno unos unas por mas tras sobre como este
esta estos estas sus entre desde hasta pero sin todo toda todos todas
fue son fueron sera seran hay hoy dia dias donde cuando quien tiene
tienen tras ante bajo cada cual cuales cuyo cuya durante segun mientras
otra otro otros otras cual cuales ser estar hacer haber cabe pues asi
tan muy mas menos ya solo dijo dice sido esa ese esos esas nos les
argentina argentino argentinos argentinas contra nueva nuevo nuevos
nuevas casa vivo video tres cuatro cinco seis siete ocho nueve diez
once doce mil millon millones miles anos york tras despues antes
enero febrero marzo abril mayo junio julio agosto septiembre octubre
noviembre diciembre lunes martes miercoles jueves viernes sabado domingo
segun luego mientras tanto asegura afirma señala confirma
""".split())


def _tokenizar(titulo: str) -> set:
    t = _normalizar(titulo)
    palabras = re.findall(r"[a-záéíóúñü]{4,}", t)
    return {p for p in palabras if p not in STOPWORDS}


def calcular_palabra_del_dia(con, posiciones, horas=24, minimo_titulares=3):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
    filas = con.execute(
        "SELECT medio, titulo FROM notas WHERE fecha_recoleccion >= ?", (cutoff,)
    ).fetchall()
    if not filas:
        return None

    contador = Counter()
    por_nota = []
    for medio, titulo in filas:
        palabras = _tokenizar(titulo)
        por_nota.append((medio, titulo, palabras))
        contador.update(palabras)

    if not contador:
        return None

    palabra, frecuencia = contador.most_common(1)[0]
    if frecuencia < minimo_titulares:
        return None

    medios_con_palabra = set()
    por_bucket = {}
    for medio, titulo, palabras in por_nota:
        if palabra not in palabras:
            continue
        medios_con_palabra.add(medio)
        bucket = bucket_de_posicion(posiciones.get(medio, ESCALA_CENTRO))
        entrada = por_bucket.setdefault(bucket, {"count": 0, "titulo": titulo, "medio": medio})
        entrada["count"] += 1

    if not por_bucket:
        return None

    max_count = max(v["count"] for v in por_bucket.values())
    columnas = []
    for b in ORDEN_BUCKETS:
        if b not in por_bucket:
            continue
        v = por_bucket[b]
        columnas.append(
            {
                "bucket": b,
                "count": v["count"],
                "pct": round(v["count"] / max_count * 100),
                "titulo": v["titulo"],
                "medio": v["medio"],
            }
        )

    return {
        "palabra": palabra,
        "frecuencia": frecuencia,
        "n_medios": len(medios_con_palabra),
        "columnas": columnas,
        "horas": horas,
    }


# --- Carga de datos ---------------------------------------------------------


def cargar_medios_confirmados():
    with open(FEEDS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    medios = data.get("confirmados", [])
    nombres = [m["medio"] for m in medios]
    posiciones = {}
    for m in medios:
        pos = m.get("posicion_editorial", ESCALA_CENTRO)
        posiciones[m["medio"]] = pos if isinstance(pos, (int, float)) else ESCALA_CENTRO
    return nombres, posiciones


def cargar_metodologia_medios():
    """Lista completa (no solo nombre + posición, como cargar_medios_confirmados)
    de los medios confirmados, para la tabla de metodología de comparacion.html:
    con qué criterio se clasificó editorialmente a cada uno y de dónde sale ese
    número, para que cualquiera pueda revisar el criterio en vez de tener que
    confiar en la ubicación en el eje sin más contexto."""
    with open(FEEDS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    medios = data.get("confirmados", [])
    resultado = []
    for m in medios:
        pos = m.get("posicion_editorial", ESCALA_CENTRO)
        pos = pos if isinstance(pos, (int, float)) else ESCALA_CENTRO
        resultado.append(
            {
                "medio": m["medio"],
                "tipo": m.get("tipo", ""),
                "posicion": pos,
                "bucket": bucket_de_posicion(pos),
                "fuente": m.get("fuente_posicion", ""),
            }
        )
    resultado.sort(key=lambda m: m["posicion"])
    return resultado


def cargar_historias(con):
    filas = con.execute(
        """
        SELECT h.id, n.medio, n.titulo, n.link, n.fecha_publicacion, n.resumen
        FROM historias h
        JOIN notas n ON n.historia_id = h.id
        ORDER BY h.id, n.medio
        """
    ).fetchall()

    historias = {}
    for hid, medio, titulo, link, fecha, resumen in filas:
        historias.setdefault(hid, []).append(
            {"medio": medio, "titulo": titulo, "link": link, "fecha": fecha, "resumen": resumen}
        )

    # Un mismo medio puede publicar más de una nota sobre la misma historia
    # (por ejemplo notas "en vivo" que se republican con una URL nueva cada
    # vez). Nos quedamos con una sola por medio y por historia — la más
    # reciente — para no inflar el indicador político ni repetir la tarjeta.
    for hid, notas in historias.items():
        por_medio = {}
        for n in notas:
            actual = por_medio.get(n["medio"])
            if actual is None or (n["fecha"] or "") > (actual["fecha"] or ""):
                por_medio[n["medio"]] = n
        historias[hid] = list(por_medio.values())

    return historias


# --- Indicador de inclinación política (strip-plot) -------------------------


def bucket_de_posicion(pos: float) -> str:
    if pos < 4.5:
        return ETIQUETAS_POSICION[1]
    if pos < 8.5:
        return ETIQUETAS_POSICION[2]
    if pos < 13.5:
        return ETIQUETAS_POSICION[3]
    if pos < 17.5:
        return ETIQUETAS_POSICION[4]
    return ETIQUETAS_POSICION[5]


def descripcion_promedio(promedio: float, n_coberturas: int) -> str:
    base = f"promedio de {n_coberturas} cobertura{'s' if n_coberturas != 1 else ''}"
    bucket = bucket_de_posicion(promedio)
    if bucket != "Centro":
        return base
    diff = abs(promedio - ESCALA_CENTRO)
    if diff < 1:
        return base
    direccion = "la derecha" if promedio > ESCALA_CENTRO else "la izquierda"
    calificador = "leve inclinación a" if diff < 2.5 else "inclinación a"
    return f"{base}, {calificador} {direccion}"


def calcular_consenso(valores):
    """Qué tan de acuerdo están, en términos de ubicación editorial, los medios
    que cubrieron una historia. No mide si el hecho es verdadero ni si hay
    coincidencia de opinión real — solo la dispersión de las posiciones
    editoriales (1-21) de quienes la cubrieron. Umbrales elegidos a ojo sobre
    la escala de 20 puntos; se pueden ajustar con más datos reales."""
    if len(valores) < 2:
        return {"etiqueta": "Cobertura única", "clase": "unica"}
    dispersion = statistics.pstdev(valores)
    if dispersion < 1.8:
        return {"etiqueta": "Consenso", "clase": "consenso"}
    if dispersion < 4.2:
        return {"etiqueta": "Cobertura mixta", "clase": "mixta"}
    return {"etiqueta": "Cobertura polarizada", "clase": "polarizada"}


def armar_indicador(notas, posiciones):
    """Strip-plot: agrupa las notas por la posición editorial de su medio,
    y calcula el promedio ponderado por cantidad de coberturas."""
    valores = [posiciones.get(n["medio"], ESCALA_CENTRO) for n in notas]
    promedio = sum(valores) / len(valores)

    grupos = {}
    for n in notas:
        pos = posiciones.get(n["medio"], ESCALA_CENTRO)
        grupos.setdefault(pos, []).append(n["medio"])

    rango = ESCALA_MAX - ESCALA_MIN

    puntos = []
    for pos, medios_en_pos in sorted(grupos.items()):
        n = len(medios_en_pos)
        puntos.append(
            {
                "x_pct": round((pos - ESCALA_MIN) / rango * 100, 2),
                "size": min(9 + 2 * (n - 1), 17),
                "titulo": ", ".join(medios_en_pos),
            }
        )

    consenso = calcular_consenso(valores)

    return {
        "promedio": promedio,
        "promedio_x_pct": round((promedio - ESCALA_MIN) / rango * 100, 2),
        "etiqueta": bucket_de_posicion(promedio),
        "descripcion": descripcion_promedio(promedio, len(notas)),
        "puntos": puntos,
        "consenso_etiqueta": consenso["etiqueta"],
        "consenso_clase": consenso["clase"],
    }


_RE_ETIQUETAS_HTML = re.compile(r"<[^>]+>")
_RE_ESPACIOS = re.compile(r"\s+")


def limpiar_resumen(texto):
    """Los resumenes vienen crudos del RSS de cada medio: algunos traen
    HTML metido adentro (hasta un <img> en el medio del texto). Se sacan
    las etiquetas y se desescapan las entidades (&amp; -> &, etc)."""
    if not texto:
        return ""
    sin_etiquetas = _RE_ETIQUETAS_HTML.sub(" ", texto)
    sin_entidades = desescapar_html(sin_etiquetas)
    return _RE_ESPACIOS.sub(" ", sin_entidades).strip()


def truncar_en_limite_natural(texto, tope):
    """Corta un texto largo tratando de terminar en una oración completa
    (si hay un punto razonablemente cerca del límite); si no, corta en el
    último espacio y agrega puntos suspensivos."""
    if len(texto) <= tope:
        return texto
    ventana = texto[:tope]
    corte_oracion = max(ventana.rfind(". "), ventana.rfind("? "), ventana.rfind("! "))
    if corte_oracion >= tope * 0.55:
        return ventana[: corte_oracion + 1].rstrip()
    corte_palabra = ventana.rsplit(" ", 1)[0]
    return corte_palabra.rstrip(",.;:") + "…"


def elegir_resumen(notas, tope=240):
    """Un resumen representativo de la cobertura para el modo swipe: no es
    una síntesis de lo que dicen todos los medios (eso requeriría IA), es
    el resumen más largo/completo entre los que mandó cada medio por RSS."""
    candidatos = []
    for n in notas:
        limpio = limpiar_resumen(n.get("resumen", ""))
        if limpio:
            candidatos.append((len(limpio), limpio, n["medio"]))
    if not candidatos:
        return None
    candidatos.sort(key=lambda c: c[0], reverse=True)
    _, texto, medio = candidatos[0]
    return {"texto": truncar_en_limite_natural(texto, tope), "medio": medio}


def armar_distribucion_lcr(notas, posiciones):
    """% de coberturas por bloque editorial ancho (izquierda incluye
    centro-izquierda, derecha incluye centro-derecha) para la barra de
    tres colores del modo swipe. Los porcentajes se redondean con el
    método del resto mayor para que siempre sumen 100."""
    conteo = {"izquierda": 0, "centro": 0, "derecha": 0}
    for n in notas:
        pos = posiciones.get(n["medio"], ESCALA_CENTRO)
        bucket = bucket_de_posicion(pos)
        if bucket in (ETIQUETAS_POSICION[1], ETIQUETAS_POSICION[2]):
            conteo["izquierda"] += 1
        elif bucket == ETIQUETAS_POSICION[3]:
            conteo["centro"] += 1
        else:
            conteo["derecha"] += 1

    total = sum(conteo.values()) or 1
    crudos = {k: v / total * 100 for k, v in conteo.items()}
    enteros = {k: int(v) for k, v in crudos.items()}
    restante = 100 - sum(enteros.values())
    por_resto = sorted(crudos, key=lambda k: crudos[k] - enteros[k], reverse=True)
    for i in range(restante):
        enteros[por_resto[i % len(por_resto)]] += 1

    return {
        "izquierda_pct": enteros["izquierda"],
        "centro_pct": enteros["centro"],
        "derecha_pct": enteros["derecha"],
    }


def armar_indicador_global(medios_confirmados, posiciones):
    """Misma lógica que armar_indicador, pero un voto por medio (para la
    franja ilustrativa de ubicación editorial de toda la redacción)."""
    notas_ficticias = [{"medio": m} for m in medios_confirmados]
    return armar_indicador(notas_ficticias, posiciones)


# --- Comparación de medios ---------------------------------------------------


def abreviar_medio(nombre: str, maxlen: int = 11) -> str:
    if len(nombre) <= maxlen:
        return nombre
    palabras = nombre.split()
    if len(palabras) > 1:
        candidato = palabras[-1]
        if 3 <= len(candidato) <= maxlen:
            return candidato
    return nombre[: maxlen - 1] + "…"


def armar_matriz_comparacion(historias_todas, medios_confirmados):
    cobertura_por_medio = defaultdict(set)
    for hid, notas in historias_todas.items():
        for n in notas:
            cobertura_por_medio[n["medio"]].add(hid)

    nombres = medios_confirmados
    totales = [len(cobertura_por_medio.get(m, set())) for m in nombres]

    matriz = []
    for mi in nombres:
        fila = []
        a = cobertura_por_medio.get(mi, set())
        for mj in nombres:
            if mi == mj:
                fila.append(None)
                continue
            b = cobertura_por_medio.get(mj, set())
            union = a | b
            inter = a & b
            pct = round(len(inter) / len(union) * 100) if union else 0
            fila.append(pct)
        matriz.append(fila)

    return nombres, totales, matriz


# --- Datos para el comparador 1 a 1 (cliente) --------------------------------


def armar_datos_comparacion(historias_todas):
    """Para cada medio, sus titulares en las historias con 2+ coberturas —
    lo mínimo para que dos medios puedan tener algo en común. Se usa en
    comparacion.html para mostrar, lado a lado, cómo tituló cada uno la
    misma historia. Las historias con una sola cobertura se descartan acá
    porque nunca pueden aparecer en un cruce entre dos medios, y así el
    JSON no carga con datos que ningún par va a usar."""
    por_medio = {}
    for hid, notas in historias_todas.items():
        if len(notas) < 2:
            continue
        for n in notas:
            por_medio.setdefault(n["medio"], {})[str(hid)] = {
                "t": n["titulo"],
                "f": n["fecha"] or "",
                "l": n["link"],
            }
    return por_medio


# --- Tendencias (datos los arma tendencias.py, esto solo los lee y dibuja) --


def _tabla_existe(con, nombre):
    fila = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (nombre,)
    ).fetchone()
    return bool(fila)


def _suavizar(valores, ventana):
    """Promedio móvil centrado, sin achicar la lista (los bordes usan el
    tramo que tienen disponible). interest_over_time de Google viene con
    bastante ruido punto a punto incluso para "now 1-d" — un poco de
    suavizado deja ver la tendencia real sin aplanar picos genuinos."""
    if ventana <= 1:
        return valores
    n = len(valores)
    mitad = ventana // 2
    return [
        sum(valores[max(0, i - mitad):min(n, i + mitad + 1)])
        / len(valores[max(0, i - mitad):min(n, i + mitad + 1)])
        for i in range(n)
    ]


def armar_sparkline_svg(puntos, ancho=280, alto=64):
    """Convierte la curva de interés de búsqueda (lista de {"t","v"}) en
    coordenadas para un sparkline SVG — línea de 2px y área rellena al
    ~10% de opacidad, como el resto de los indicadores del sitio (ver
    _estilos_base.html). None si no hay suficientes puntos para que una
    línea tenga sentido (por ejemplo, si a ese tema no se le pudo traer
    la curva esta corrida)."""
    if not puntos or len(puntos) < 2:
        return None
    n = len(puntos)
    ventana = 5 if n >= 24 else (3 if n >= 12 else 1)
    valores = _suavizar([p["v"] for p in puntos], ventana)
    minimo, maximo = min(valores), max(valores)
    rango = (maximo - minimo) or 1
    margen = 3

    coords = []
    for i, v in enumerate(valores):
        x = i / (n - 1) * ancho
        y = alto - ((v - minimo) / rango * (alto - margen * 2)) - margen
        coords.append(f"{x:.1f},{y:.1f}")

    linea = " ".join(coords)
    area = f"0,{alto} {linea} {ancho},{alto}"
    return {"ancho": ancho, "alto": alto, "linea": linea, "area": area}


def cargar_tendencias(con):
    """Lee el último resultado que dejó tendencias.py en noticias.db
    (fresco o, si Google falló esa corrida, el último que funcionó — la
    degradación ya la resuelve tendencias.py, acá solo se lee y se le
    agrega el SVG). None si tendencias.py nunca corrió con éxito."""
    if not _tabla_existe(con, "estado_pipeline"):
        return None
    fila = con.execute(
        "SELECT valor FROM estado_pipeline WHERE clave = 'tendencias_render'"
    ).fetchone()
    if not fila:
        return None
    try:
        datos = json.loads(fila[0])
    except (json.JSONDecodeError, TypeError):
        return None
    for t in datos.get("tendencias", []):
        t["curva_svg"] = armar_sparkline_svg(t.get("curva"))
    return datos


# --- Armado de contexto -------------------------------------------------------


def texto_medios_faltan(medios_faltan, tope=6):
    if not medios_faltan:
        return ""
    if len(medios_faltan) <= tope:
        return ", ".join(medios_faltan)
    resto = len(medios_faltan) - tope
    return ", ".join(medios_faltan[:tope]) + f" y {resto} medio{'s' if resto != 1 else ''} más"


def armar_contexto(historias, medios_confirmados, posiciones):
    resultado = []
    for hid, notas in historias.items():
        medios_cubrieron = sorted({n["medio"] for n in notas})
        if len(medios_cubrieron) < MEDIOS_MINIMOS:
            continue
        for n in notas:
            n["bucket"] = bucket_de_posicion(posiciones.get(n["medio"], ESCALA_CENTRO))
        medios_faltan = [m for m in medios_confirmados if m not in medios_cubrieron]
        resultado.append(
            {
                "id": hid,
                "titulo": notas[0]["titulo"],
                "categoria": categoria_de_historia(notas[0]["titulo"]),
                "notas": notas,
                "n_medios": len(medios_cubrieron),
                "medios_faltan": medios_faltan,
                "medios_faltan_texto": texto_medios_faltan(medios_faltan),
                "indicador": armar_indicador(notas, posiciones),
                "resumen_representativo": elegir_resumen(notas),
                "distribucion_lcr": armar_distribucion_lcr(notas, posiciones),
            }
        )
    # más medios primero (lo más compartido arriba); entre empatados, más notas primero
    resultado.sort(key=lambda h: (-h["n_medios"], -len(h["notas"])))
    for i, h in enumerate(resultado):
        h["destacada"] = i == 0
    return resultado


def main():
    medios_confirmados, posiciones = cargar_medios_confirmados()

    con = sqlite3.connect(DB_PATH)
    historias = cargar_historias(con)
    palabra_del_dia = calcular_palabra_del_dia(con, posiciones)
    datos_tendencias = cargar_tendencias(con)
    con.close()

    if not historias:
        print("No hay historias agrupadas todavía. Corré primero recolector.py y agrupar.py.")
        return

    contexto_historias = armar_contexto(historias, medios_confirmados, posiciones)

    if not contexto_historias:
        print(
            f"Ninguna historia llegó al mínimo de {MEDIOS_MINIMOS} medios distintos todavía "
            "(puede pasar con pocos medios confirmados o poco tiempo de recolección)."
        )

    generado = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    indicador_global = armar_indicador_global(medios_confirmados, posiciones)
    historia_destacada = contexto_historias[0] if contexto_historias else None

    env = Environment(loader=FileSystemLoader("."), autoescape=True)

    # --- Home ---
    plantilla = env.get_template("plantilla.html")
    html = plantilla.render(
        historias=contexto_historias,
        total_medios=len(medios_confirmados),
        generado=generado,
        medios_minimos=MEDIOS_MINIMOS,
        categorias=CATEGORIAS,
        palabra_del_dia=palabra_del_dia,
        indicador_global=indicador_global,
        historia_destacada=historia_destacada,
    )
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8") as f:
        f.write(html)

    # --- Páginas de historia ---
    os.makedirs(DIR_HISTORIAS, exist_ok=True)
    plantilla_historia = env.get_template("historia.html")
    ids_vigentes = set()
    for h in contexto_historias:
        html_h = plantilla_historia.render(h=h, generado=generado, total_medios=len(medios_confirmados))
        with open(os.path.join(DIR_HISTORIAS, f"{h['id']}.html"), "w", encoding="utf-8") as f:
            f.write(html_h)
        ids_vigentes.add(f"{h['id']}.html")

    # Borra páginas de historias que ya no están vigentes (dejaron de tener 3+ medios
    # o se re-agruparon con otro id), para no acumular páginas huérfanas.
    huerfanas = 0
    for nombre in os.listdir(DIR_HISTORIAS):
        if nombre.endswith(".html") and nombre not in ids_vigentes:
            os.remove(os.path.join(DIR_HISTORIAS, nombre))
            huerfanas += 1

    # --- Comparación de medios ---
    nombres_matriz, totales_matriz, matriz = armar_matriz_comparacion(historias, medios_confirmados)
    metodologia_medios = cargar_metodologia_medios()
    plantilla_comparacion = env.get_template("comparacion.html")
    html_c = plantilla_comparacion.render(
        nombres=nombres_matriz,
        abreviados=[abreviar_medio(n) for n in nombres_matriz],
        totales=totales_matriz,
        matriz=matriz,
        posiciones=posiciones,
        metodologia=metodologia_medios,
        generado=generado,
    )
    with open(SALIDA_COMPARACION, "w", encoding="utf-8") as f:
        f.write(html_c)

    # --- Datos para el comparador 1 a 1 (JSON que consume comparacion.html) ---
    datos_comparacion = armar_datos_comparacion(historias)
    with open(SALIDA_DATOS_COMPARACION, "w", encoding="utf-8") as f:
        json.dump(datos_comparacion, f, ensure_ascii=False, separators=(",", ":"))

    # --- Nosotros ---
    plantilla_nosotros = env.get_template("nosotros.html")
    html_n = plantilla_nosotros.render()
    with open(SALIDA_NOSOTROS, "w", encoding="utf-8") as f:
        f.write(html_n)

    # --- Guardadas (la página en sí no lleva datos: filtra index.html en el navegador) ---
    plantilla_guardadas = env.get_template("guardadas.html")
    html_g = plantilla_guardadas.render()
    with open(SALIDA_GUARDADAS, "w", encoding="utf-8") as f:
        f.write(html_g)

    # --- Tendencias ---
    plantilla_tendencias = env.get_template("tendencias.html")
    html_t = plantilla_tendencias.render(
        tendencias=(datos_tendencias or {}).get("tendencias", []),
        generado_tendencias=(datos_tendencias or {}).get("generado"),
        desde_cache=(datos_tendencias or {}).get("desde_cache", False),
        generado=generado,
    )
    with open(SALIDA_TENDENCIAS, "w", encoding="utf-8") as f:
        f.write(html_t)

    n_tendencias = len((datos_tendencias or {}).get("tendencias", []))
    print(
        f"Listo: {SALIDA} generado con {len(contexto_historias)} historias "
        f"(de {len(historias)} agrupadas, filtrando por {MEDIOS_MINIMOS}+ medios). "
        f"{len(contexto_historias)} páginas de historia + comparación de medios + "
        f"{n_tendencias} tendencias. {huerfanas} páginas de historia vieja(s) borrada(s)."
    )


if __name__ == "__main__":
    main()
