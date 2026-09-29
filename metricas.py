"""
Ground News Argentina — métricas de sesgo sin LLM (etapa 7, prototipo)

Calcula, por medio, tres métricas que no dependen de ningún LLM (ver la
revisión "Decisión: mismas métricas de salida, sin LLM" en el doc del
proyecto para el razonamiento completo detrás de cada una):

- bias_intensity (0-100): combina (a) distancia de framing —qué tan
  lejos titula un medio respecto al promedio de LOS OTROS medios que
  cubrieron la misma historia (leave-one-out), medido con el mismo
  modelo de embeddings que usa agrupar.py— y (b) densidad de lenguaje
  cargado según lexico_sesgo.yaml. Mide intensidad, no dirección.

  DOS CORRECCIONES aplicadas después de la primera corrida real (Max,
  22/09/2026), que mostró Buenos Aires Times y MercoPress primeros y
  Página/12 y La Izquierda Diario últimos — justo al revés de lo
  esperable:

  1) Sesgo de idioma: el modelo multilingüe deja una distancia residual
     entre un título en inglés y el promedio de títulos en español de
     la misma historia, aunque digan lo mismo — eso, no el contenido,
     era lo que hacía subir a BA Times/MercoPress. Se detectan por el
     campo "tipo" en feeds.yaml (contiene "inglés") y se excluyen del
     cálculo de distancia de framing, tanto como comparados como como
     parte del centroide de los demás. Su bias_intensity queda en None
     ("no medido en esta versión") en vez de un número engañoso.

  2) Rango comprimido: antes se usaba un tope fijo (*100) sin calibrar
     contra datos reales, y el resultado quedaba apretado entre 8 y 15
     sobre 100. Ahora se normaliza min-max contra la distribución real
     de distancias observadas en la corrida, así se usa el rango
     0-100 entero y el métrico discrimina mejor entre medios.

- consistencia_cobertura (0-100): combina qué % de las historias del
  período cubrió el medio, y qué tan pareja es su cadencia de
  publicación. No mide exactitud factual — ver la revisión del PDF en
  el doc para el porqué.

- confidence (0-100): combina N (historias consideradas para ese
  medio) y dispersión del bias_intensity historia a historia. Si un
  medio no tiene bias_intensity calculado (medios en inglés, por
  ahora), su confidence queda en 0 — no hay nada de qué estar
  confiado.

Los pesos 0.6/0.4 entre framing y lenguaje cargado siguen siendo
provisorios (marcados "AJUSTAR") — con el rango ya calibrado, tiene
sentido revisarlos de nuevo mirando la próxima corrida.

Guarda un snapshot por corrida en la tabla `metricas_medio` de
noticias.db (no pisa corridas anteriores, así se puede ver evolución).

Uso:
    python metricas.py
    python metricas.py --dias 7
"""

import argparse
import sqlite3
import statistics
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import yaml
from sentence_transformers import SentenceTransformer

DB_PATH = "noticias.db"
LEXICO_PATH = "lexico_sesgo.yaml"
FEEDS_PATH = "feeds.yaml"
MODELO = "paraphrase-multilingual-MiniLM-L12-v2"


def preparar_db(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS metricas_medio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medio TEXT NOT NULL,
            calculado TEXT NOT NULL,
            periodo_desde TEXT NOT NULL,
            periodo_hasta TEXT NOT NULL,
            n_historias INTEGER NOT NULL,
            bias_intensity REAL,
            consistencia_cobertura REAL,
            confidence REAL
        )
        """
    )
    con.commit()


def _normalizar(txt):
    txt = txt.lower()
    return "".join(c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn")


def cargar_lexico():
    with open(LEXICO_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [_normalizar(t["termino"]) for t in data.get("terminos", [])]


def cargar_medios_no_espanol():
    """Medios cuyo campo 'tipo' en feeds.yaml indica que titulan en otro
    idioma (hoy: inglés) — se excluyen del cálculo de distancia de
    framing porque el modelo multilingüe infla artificialmente su
    distancia contra un centroide en español."""
    with open(FEEDS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    no_espanol = set()
    for m in data.get("confirmados", []):
        tipo = (m.get("tipo") or "").lower()
        if "ingles" in _normalizar(tipo):
            no_espanol.add(m["medio"])
    return no_espanol


def puntaje_lenguaje_cargado(titulo, terminos):
    """Términos cargados por cada 100 palabras del título."""
    t = _normalizar(titulo)
    palabras = max(len(t.split()), 1)
    hits = sum(1 for term in terminos if term in t)
    return (hits / palabras) * 100


def cargar_notas_periodo(con, dias):
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    filas = con.execute(
        """
        SELECT n.historia_id, n.medio, n.titulo, n.fecha_recoleccion
        FROM notas n
        WHERE n.historia_id IS NOT NULL AND n.fecha_recoleccion >= ?
        ORDER BY n.historia_id, n.fecha_recoleccion
        """,
        (desde,),
    ).fetchall()

    # una nota por medio por historia (la más reciente) — mismo criterio
    # de deduplicación que usa generar_pagina.py para TN y similares
    historias = defaultdict(dict)
    for hid, medio, titulo, fecha in filas:
        actual = historias[hid].get(medio)
        if actual is None or fecha > actual["fecha"]:
            historias[hid][medio] = {"titulo": titulo, "fecha": fecha}
    return historias, desde


def calcular_bias_intensity(historias, modelo, terminos, medios_no_espanol):
    pares = []
    for hid, por_medio in historias.items():
        for medio, info in por_medio.items():
            pares.append((hid, medio, info["titulo"]))

    if not pares:
        return {}

    titulos = [p[2] for p in pares]
    embeddings = modelo.encode(titulos, normalize_embeddings=True, show_progress_bar=True)

    por_historia = defaultdict(list)
    for (hid, medio, titulo), emb in zip(pares, embeddings):
        por_historia[hid].append((medio, emb, titulo))

    # primera pasada: distancia de framing cruda, SOLO entre medios en
    # español (excluye del centroide y de la comparación a los medios
    # detectados como no-español, para no inflar ni contaminar nada)
    dist_cruda = {}  # (hid, medio) -> distancia sin normalizar
    titulo_de = {}
    for hid, items in por_historia.items():
        items_es = [(m, e, t) for m, e, t in items if m not in medios_no_espanol]
        if len(items_es) < 2:
            continue  # sin otro medio en español para comparar
        suma = np.sum([e for _, e, _ in items_es], axis=0)
        for medio, emb, titulo in items_es:
            centroide_otros = (suma - emb) / (len(items_es) - 1)
            norm = np.linalg.norm(centroide_otros)
            if norm > 0:
                centroide_otros = centroide_otros / norm
            dist = 1 - float(np.dot(emb, centroide_otros))
            dist_cruda[(hid, medio)] = max(dist, 0.0)
            titulo_de[(hid, medio)] = titulo

    if not dist_cruda:
        return {}

    # segunda pasada: normalizar min-max contra la distribución real de
    # ESTA corrida, para usar el rango 0-100 entero en vez de un tope fijo
    valores = list(dist_cruda.values())
    d_min, d_max = min(valores), max(valores)
    rango = (d_max - d_min) or 1.0

    resultado = {}
    for clave, dist in dist_cruda.items():
        dist_framing_pct = (dist - d_min) / rango * 100
        carga = puntaje_lenguaje_cargado(titulo_de[clave], terminos)
        carga_pct = min(carga * 15, 100)  # AJUSTAR: revisar de nuevo con el rango ya calibrado

        bi = 0.6 * dist_framing_pct + 0.4 * carga_pct  # AJUSTAR: pesos provisorios
        resultado[clave] = bi

    return resultado


def calcular_metricas(historias, bias_por_par, total_historias, medios_no_espanol):
    por_medio_bi = defaultdict(list)
    por_medio_historias = defaultdict(set)
    fechas_por_medio = defaultdict(list)

    for hid, por_medio in historias.items():
        for medio, info in por_medio.items():
            por_medio_historias[medio].add(hid)
            fechas_por_medio[medio].append(info["fecha"])

    for (hid, medio), bi in bias_por_par.items():
        por_medio_bi[medio].append(bi)

    filas = []
    for medio in sorted(fechas_por_medio):
        n_hist = len(por_medio_historias[medio])
        bis = por_medio_bi.get(medio, [])
        if medio in medios_no_espanol:
            bias_intensity = None  # no medido en esta versión, ver docstring
        else:
            bias_intensity = round(statistics.mean(bis), 2) if bis else None
        dispersion = statistics.pstdev(bis) if len(bis) > 1 else 0.0

        # consistencia de cobertura
        pct_cobertura = (n_hist / total_historias * 100) if total_historias else 0
        dias_con_notas = defaultdict(int)
        for f in fechas_por_medio[medio]:
            dias_con_notas[f[:10]] += 1
        conteos = list(dias_con_notas.values())
        if len(conteos) > 1 and statistics.mean(conteos) > 0:
            cv = statistics.pstdev(conteos) / statistics.mean(conteos)
            cadencia_pct = max(0, 100 - cv * 100)
        else:
            cadencia_pct = 50  # AJUSTAR: valor neutro cuando no hay suficientes días para medir varianza
        consistencia = min(round(0.7 * pct_cobertura + 0.3 * cadencia_pct, 2), 100)

        # confidence — usa n_muestras (cuántas historias realmente entraron
        # al promedio de bias_intensity), NO n_hist (cobertura total del
        # medio). Antes usaba n_hist por error: un medio con mucha
        # cobertura pero pocas historias compartidas con otros medios en
        # español (ej. La Izquierda Diario: 40 historias cubiertas, pero
        # solo 2 con otro medio en español al lado) mostraba confidence
        # alta cuando el promedio de bias_intensity en realidad se apoyaba
        # en casi nada.
        n_muestras = len(bis)
        n_pct = min(n_muestras / 8 * 100, 100)  # AJUSTAR: 8 muestras como referencia de "N razonable", revisar con más datos
        disp_pct = max(0, 100 - dispersion)
        confidence = round(0.5 * n_pct + 0.5 * disp_pct, 2) if bis and bias_intensity is not None else 0.0

        filas.append({
            "medio": medio,
            "n_historias": n_hist,
            "n_muestras_bias": len(bis),
            "bias_intensity": bias_intensity,
            "consistencia_cobertura": consistencia,
            "confidence": confidence,
        })
    return filas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=7, help="ventana de tiempo a considerar")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    preparar_db(con)

    terminos = cargar_lexico()
    medios_no_espanol = cargar_medios_no_espanol()
    historias, desde = cargar_notas_periodo(con, args.dias)

    if not historias:
        print("No hay historias en el período. Corré recolector.py y agrupar.py primero.")
        return

    print(f"Cargando modelo ({MODELO})...")
    modelo = SentenceTransformer(MODELO)

    print(f"Calculando métricas sobre {len(historias)} historias ({args.dias} días)...")
    if medios_no_espanol:
        print(f"Medios excluidos de bias_intensity por idioma: {', '.join(sorted(medios_no_espanol))}")
    bias_por_par = calcular_bias_intensity(historias, modelo, terminos, medios_no_espanol)
    filas = calcular_metricas(historias, bias_por_par, len(historias), medios_no_espanol)

    ahora = datetime.now(timezone.utc).isoformat()
    con.executemany(
        """
        INSERT INTO metricas_medio
            (medio, calculado, periodo_desde, periodo_hasta, n_historias, bias_intensity, consistencia_cobertura, confidence)
        VALUES (:medio, :calculado, :desde, :hasta, :n_historias, :bias_intensity, :consistencia_cobertura, :confidence)
        """,
        [{**f, "calculado": ahora, "desde": desde, "hasta": ahora} for f in filas],
    )
    con.commit()

    print(f"\n{len(filas)} medios procesados.\n")
    print(f"{'medio':<22} {'n_hist':>7} {'n_bias':>7} {'bias_int':>9} {'consist':>8} {'confid':>7}")
    for f in sorted(filas, key=lambda f: -(f["bias_intensity"] if f["bias_intensity"] is not None else -1)):
        bi = f["bias_intensity"] if f["bias_intensity"] is not None else "sin medir"
        print(f"{f['medio']:<22} {f['n_historias']:>7} {f['n_muestras_bias']:>7} {bi!s:>9} {f['consistencia_cobertura']:>8} {f['confidence']:>7}")
    print("\n'n_bias' = cuántas historias entraron al promedio de bias_intensity (bajo = poco confiable, mirá 'confid').")

    con.close()


if __name__ == "__main__":
    main()
