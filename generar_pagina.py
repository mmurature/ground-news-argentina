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

Requiere haber corrido antes recolector.py y agrupar.py.

Uso:
    python generar_pagina.py
"""

import os
import re
import sqlite3
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


def cargar_historias(con):
    filas = con.execute(
        """
        SELECT h.id, n.medio, n.titulo, n.link, n.fecha_publicacion
        FROM historias h
        JOIN notas n ON n.historia_id = h.id
        ORDER BY h.id, n.medio
        """
    ).fetchall()

    historias = {}
    for hid, medio, titulo, link, fecha in filas:
        historias.setdefault(hid, []).append(
            {"medio": medio, "titulo": titulo, "link": link, "fecha": fecha}
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

    return {
        "promedio": promedio,
        "promedio_x_pct": round((promedio - ESCALA_MIN) / rango * 100, 2),
        "etiqueta": bucket_de_posicion(promedio),
        "descripcion": descripcion_promedio(promedio, len(notas)),
        "puntos": puntos,
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

    env = Environment(loader=FileSystemLoader("."))

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
    plantilla_comparacion = env.get_template("comparacion.html")
    html_c = plantilla_comparacion.render(
        nombres=nombres_matriz,
        abreviados=[abreviar_medio(n) for n in nombres_matriz],
        totales=totales_matriz,
        matriz=matriz,
        posiciones=posiciones,
        generado=generado,
    )
    with open(SALIDA_COMPARACION, "w", encoding="utf-8") as f:
        f.write(html_c)

    print(
        f"Listo: {SALIDA} generado con {len(contexto_historias)} historias "
        f"(de {len(historias)} agrupadas, filtrando por {MEDIOS_MINIMOS}+ medios). "
        f"{len(contexto_historias)} páginas de historia + comparación de medios. "
        f"{huerfanas} páginas de historia vieja(s) borrada(s)."
    )


if __name__ == "__main__":
    main()
