"""
Ground News Argentina — tendencias (etapa 3.5, entre agrupar.py y generar_pagina.py)

Trae los temas en tendencia de búsqueda en Argentina (Google Trends, vía la
librería no oficial `trendspy`) y los cruza con las historias que el
agrupador acaba de armar: qué tendencias tienen cobertura nuestra, cuáles
historias les corresponden y quién las cubrió.

Google no tiene una API pública abierta para esto (la oficial está en alpha
cerrada, sin acceso libre todavía), así que esto depende de un endpoint no
documentado que puede romperse, cambiar de forma o empezar a bloquear sin
aviso — exactamente lo que le pasó a `pytrends`, la librería que se usaba
antes de `trendspy`. Por eso todo acá adentro está armado para degradar
solo, no para tirar abajo el resto del pipeline:

  - Si falla traer la lista de tendencias de Google, se reusa la última
    que funcionó (queda cacheada en noticias.db, tabla `estado_pipeline`).
  - El cruce con nuestras historias SIEMPRE se recalcula de cero contra la
    tabla `historias` actual (agrupar.py la rearma entera en cada corrida,
    los ids no son estables entre corridas) — nunca se cachea ese cruce.
  - Si falla la curva de interés de búsqueda (`interest_over_time`) de un
    tema puntual, ese tema se queda sin gráfico pero conserva el resto
    (nombre, volumen aproximado, historias relacionadas). Esta es la
    llamada más frágil y la más lenta, así que solo se pide para los temas
    que ya sabemos que van a mostrarse destacados (el más buscado + los
    que tienen alguna historia nuestra relacionada), nunca para los quince
    de la lista completa.

IMPORTANTE — esto no se pudo probar contra Google Trends real: el sandbox
donde se escribió este script no tiene salida de red hacia trends.google.com
(mismo motivo por el que no se pudieron validar feeds nuevos ni traer logos
en su momento). La primera corrida real en GitHub Actions es la que va a
decir si `trendspy` sigue funcionando tal cual hoy y si los nombres de
campo que se usan acá (`_campo_en`, más abajo) son los correctos — si algo
sale raro, revisar el log de esa corrida: imprime el objeto crudo que
devuelve `trending_now()` la primera vez que corre, con sus atributos, así
se puede ajustar `_campo_en` sin tener que adivinar de nuevo.

Uso:
    python tendencias.py
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

from generar_pagina import (
    armar_distribucion_lcr,
    armar_indicador,
    cargar_medios_confirmados,
    categoria_de_historia,
)

DB_PATH = "noticias.db"
GEO = "AR"
MAX_TENDENCIAS = 15  # cuántos temas traer de Google Trends
MAX_CURVAS = 8       # a cuántos, como mucho, pedirles interest_over_time esta corrida
UMBRAL_MATCH = 0.55  # similitud semántica mínima para relacionar una tendencia con una historia.
# Subido de 0.42 tras ver la primera corrida real: con 0.42 había falsos positivos claros
# ("muerte" matcheaba con cinco historias sin relación entre sí; "walter samuel" matcheaba
# con una nota sobre un santo). 0.55 es todavía una estimación, no un valor medido — el log
# de [match] que emite emparejar_tendencias_con_historias() imprime el top-3 de similitud
# real por tendencia (pase o no el umbral) para poder terminar de calibrarlo con esos números.
MODELO_EMBEDDINGS = "paraphrase-multilingual-MiniLM-L12-v2"  # mismo modelo que usa agrupar.py — no hay acoplamiento real (cada script corre por separado), es solo la misma elección por calidad ya probada


# --- Caché en noticias.db, para degradar sin romper nada -------------------


def preparar_cache(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS estado_pipeline (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL,
            actualizada TEXT NOT NULL
        )
        """
    )
    con.commit()


def leer_cache(con, clave):
    fila = con.execute(
        "SELECT valor, actualizada FROM estado_pipeline WHERE clave = ?", (clave,)
    ).fetchone()
    if not fila:
        return None, None
    try:
        return json.loads(fila[0]), fila[1]
    except (json.JSONDecodeError, TypeError):
        return None, None


def guardar_cache(con, clave, valor):
    ahora = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO estado_pipeline (clave, valor, actualizada) VALUES (?, ?, ?)
        ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor, actualizada = excluded.actualizada
        """,
        (clave, json.dumps(valor, ensure_ascii=False), ahora),
    )
    con.commit()


# --- Traer datos de Google Trends (la parte frágil) -------------------------


def _campo_en(obj, *nombres, default=None):
    """trendspy no documenta la forma exacta de lo que devuelve
    trending_now() (a diferencia de trending_now_by_rss, que sí) — prueba
    varios nombres de atributo/clave posibles en vez de asumir uno solo,
    para no romper todo el paso si el nombre real es otro."""
    for nombre in nombres:
        if hasattr(obj, nombre):
            val = getattr(obj, nombre)
            if val not in (None, ""):
                return val
        if isinstance(obj, dict) and obj.get(nombre) not in (None, ""):
            return obj[nombre]
    return default


def formatear_volumen(valor):
    """Google Trends da el volumen como un entero ya redondeado a un
    escalón (20000, 50000, 1000000...), no como texto legible — esto lo
    pasa a algo tipo "20 mil+" (que es, de hecho, lo que muestra la propia
    UI de Google). Si en algún momento el campo real resulta ser uno de
    los que ya viene como texto (`formatted_traffic`/`traffic`), se
    devuelve tal cual en vez de intentar reinterpretarlo como número."""
    if valor in (None, ""):
        return None
    numero = None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        numero = valor
    elif isinstance(valor, str):
        texto = valor.strip()
        if texto.isdigit():
            numero = int(texto)
        else:
            return texto or None
    if numero is None or numero <= 0:
        return None

    def _sin_cero_final(texto):
        return texto[:-2] if texto.endswith(",0") else texto

    if numero >= 1_000_000:
        return f"{_sin_cero_final(f'{numero / 1_000_000:.1f}'.replace('.', ','))} M+"
    if numero >= 1_000:
        cociente = numero / 1_000
        texto = f"{cociente:.0f}" if cociente >= 10 else _sin_cero_final(f"{cociente:.1f}".replace(".", ","))
        return f"{texto} mil+"
    return f"{int(numero)}+"


def traer_tendencias_google(geo=GEO, cuantas=MAX_TENDENCIAS):
    """Lista de dicts {"query": str, "volumen_texto": str|None} con los
    temas en tendencia ahora mismo en Argentina, o None si falló por
    completo (sin internet hacia Google, cambió el endpoint, error de la
    librería, lo que sea). Nunca tira excepción hacia afuera."""
    try:
        from trendspy import Trends
    except ImportError:
        print("  trendspy no está instalado (¿faltó agregarlo a requirements.txt?).")
        return None

    try:
        tr = Trends()
        crudos = tr.trending_now(geo=geo)
    except Exception as e:
        print(f"  ERROR al traer trending_now de Google Trends: {e}")
        return None

    if not crudos:
        print("  trending_now devolvió vacío.")
        return None

    primero = crudos[0]
    detalle = vars(primero) if hasattr(primero, "__dict__") else primero
    print(f"  DEBUG primer resultado crudo de Trends (para chequear nombres de campo): {detalle!r}")

    resultado = []
    for item in crudos[:cuantas]:
        query = _campo_en(item, "keyword", "query", "term", "topic", "title")
        if not query:
            query = str(item)
        volumen = _campo_en(item, "volume", "search_volume", "formatted_traffic", "traffic")
        resultado.append(
            {
                "query": str(query).strip(),
                "volumen_texto": formatear_volumen(volumen),
            }
        )
    return resultado


def traer_curva(tr, query, timeframe="now 1-d"):
    """Curva de interés de búsqueda de las últimas 24hs para un tema
    puntual: lista de {"t": iso8601, "v": float} o None si falló o vino
    vacía. La llamada más pesada y más propensa a que Google la bloquee
    si se hacen muchas seguidas — por eso se limita quién la pide (ver
    MAX_CURVAS en main()) y se espera un poco entre cada una."""
    try:
        df = tr.interest_over_time(query, timeframe=timeframe)
    except Exception as e:
        print(f"    sin curva para {query!r}: {e}")
        return None

    if df is None or df.empty:
        return None

    columnas_valor = [c for c in df.columns if str(c).lower() != "ispartial"]
    if not columnas_valor:
        return None
    col = columnas_valor[0]

    puntos = []
    for idx, valor in df[col].items():
        try:
            ts = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
            puntos.append({"t": ts, "v": float(valor)})
        except (TypeError, ValueError):
            continue
    return puntos or None


# --- Cruce con nuestras historias (siempre fresco, nunca cacheado) ---------


def cargar_historias_actuales(con):
    """Todas las historias que el agrupador acaba de armar en esta misma
    corrida, sin filtrar por cantidad de medios: acá interesa mostrar
    incluso una cobertura incipiente de un solo medio ("esto es tendencia
    y todavía nadie lo cubrió" es, en sí, un dato interesante)."""
    filas = con.execute(
        """
        SELECT h.id, h.titulo_representativo, n.medio, n.titulo, n.link, n.fecha_publicacion
        FROM historias h
        JOIN notas n ON n.historia_id = h.id
        ORDER BY h.id
        """
    ).fetchall()
    historias = {}
    for hid, titulo_rep, medio, titulo, link, fecha in filas:
        h = historias.setdefault(hid, {"id": hid, "titulo": titulo_rep, "notas": []})
        h["notas"].append({"medio": medio, "titulo": titulo, "link": link, "fecha": fecha})
    return list(historias.values())


def enriquecer_historia(h, posiciones):
    """Misma forma que usa el resto del sitio (indicador político,
    distribución LCR, categoría) para que una historia relacionada con
    una tendencia se vea consistente con el resto de Puente."""
    notas = h["notas"]
    return {
        "id": h["id"],
        "titulo": h["titulo"],
        "categoria": categoria_de_historia(h["titulo"]),
        "n_medios": len({n["medio"] for n in notas}),
        "notas": notas,
        "indicador": armar_indicador(notas, posiciones),
        "distribucion_lcr": armar_distribucion_lcr(notas, posiciones),
    }


def emparejar_tendencias_con_historias(tendencias, historias, posiciones, umbral=UMBRAL_MATCH):
    """Para cada tendencia, qué historias nuestras hablan de lo mismo.
    Matchea por similitud semántica (embeddings) en vez de por texto
    exacto, porque una búsqueda en tendencia suele ser una frase corta
    ("Boca Juniors", "Milei Francia") y un título de historia es una
    oración completa — la comparación textual literal fallaría seguido."""
    if not tendencias:
        return tendencias
    for t in tendencias:
        t["historias"] = []
    if not historias:
        return tendencias

    from sentence_transformers import SentenceTransformer

    modelo = SentenceTransformer(MODELO_EMBEDDINGS)
    queries = [t["query"] for t in tendencias]
    titulos = [h["titulo"] for h in historias]

    emb_queries = modelo.encode(queries, normalize_embeddings=True)
    emb_historias = modelo.encode(titulos, normalize_embeddings=True)

    cache_enriquecidas = {}
    for i, t in enumerate(tendencias):
        similitudes = emb_historias @ emb_queries[i]
        ranking = sorted(
            ((float(sim), j) for j, sim in enumerate(similitudes)), key=lambda par: -par[0]
        )

        # Log de diagnóstico permanente: el top-3 de similitud real para esta
        # tendencia, pase o no el umbral (✓/✗), para poder calibrar
        # UMBRAL_MATCH con números reales mirando el log de Actions en vez de
        # adivinar — esto fue lo que permitió detectar que 0.42 daba falsos
        # positivos en la primera corrida real.
        mejores = ranking[:3]
        if mejores:
            resumen = ", ".join(
                f"{sim:.2f}{'✓' if sim >= umbral else '✗'} {historias[j]['titulo'][:60]!r}"
                for sim, j in mejores
            )
        else:
            resumen = "(sin historias para comparar)"
        print(f"  [match] {t['query']!r} -> {resumen}")

        candidatas = [(sim, j) for sim, j in ranking if sim >= umbral]
        relacionadas = []
        for _, j in candidatas[:5]:
            hid = historias[j]["id"]
            if hid not in cache_enriquecidas:
                cache_enriquecidas[hid] = enriquecer_historia(historias[j], posiciones)
            relacionadas.append(cache_enriquecidas[hid])
        t["historias"] = relacionadas

    return tendencias


# --- Orquestación ------------------------------------------------------------


def main():
    con = sqlite3.connect(DB_PATH)
    preparar_cache(con)
    _, posiciones = cargar_medios_confirmados()

    crudas = traer_tendencias_google()
    desde_cache = False
    if crudas is None:
        crudas, actualizada = leer_cache(con, "tendencias_crudas")
        if crudas:
            desde_cache = True
            print(f"  No se pudo traer la lista de Google esta corrida; uso la última que funcionó (de {actualizada}).")
        else:
            crudas = []
            print("  No hay tendencias ni de Google ni en caché todavía — la pestaña va a salir vacía esta corrida.")
    else:
        guardar_cache(con, "tendencias_crudas", crudas)

    tendencias = [dict(t) for t in crudas]  # copia: no mezclar lo que se guarda en caché con lo enriquecido acá abajo
    historias = cargar_historias_actuales(con)
    emparejar_tendencias_con_historias(tendencias, historias, posiciones)

    if tendencias:
        vistos = set()
        prioridad = ([tendencias[0]] if tendencias else []) + [t for t in tendencias if t["historias"]]
        candidatos_curva = []
        for t in prioridad:
            if t["query"] not in vistos:
                vistos.add(t["query"])
                candidatos_curva.append(t)
        candidatos_curva = candidatos_curva[:MAX_CURVAS]

        try:
            from trendspy import Trends

            tr = Trends()
        except Exception as e:
            print(f"  No se pudo inicializar Trends() para las curvas: {e}")
            tr = None

        if tr:
            for t in candidatos_curva:
                t["curva"] = traer_curva(tr, t["query"])
                time.sleep(1)  # no golpear el endpoint de Google demasiado rápido seguido

    for t in tendencias:
        t.setdefault("curva", None)
        t.setdefault("historias", [])

    # el orden se mantiene tal cual lo devuelve Google (ya viene por
    # volumen de búsqueda) — la más buscada va destacada, haya o no
    # historia nuestra relacionada con ella, ese hueco vacío es en sí
    # informativo ("esto es tendencia y todavía no lo cubrió nadie")
    for i, t in enumerate(tendencias):
        t["destacada"] = i == 0

    guardar_cache(
        con,
        "tendencias_render",
        {
            "generado": datetime.now(timezone.utc).isoformat(),
            "desde_cache": desde_cache,
            "tendencias": tendencias,
        },
    )
    con.close()

    con_historias = sum(1 for t in tendencias if t["historias"])
    con_curva = sum(1 for t in tendencias if t["curva"])
    print(
        f"Listo: {len(tendencias)} tendencias procesadas"
        f"{' (desde caché)' if desde_cache else ''}, "
        f"{con_historias} con historias relacionadas, {con_curva} con curva de búsqueda."
    )


if __name__ == "__main__":
    main()
