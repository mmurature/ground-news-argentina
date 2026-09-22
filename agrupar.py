"""
Ground News Argentina — agrupador de historias (etapa 3)

Toma las notas guardadas en noticias.db, calcula un embedding del título
de cada una y agrupa las que hablan del mismo hecho por similitud de
coseno. Guarda el resultado en la tabla `historias` y en la columna
`historia_id` de `notas`, y muestra un resumen para revisar a mano.

Uso:
    python agrupar.py
    python agrupar.py --horas 48 --umbral 0.78

Primera vez que corre: descarga el modelo de embeddings (unos 470 MB),
tarda un rato. Las siguientes corridas son rápidas porque el modelo
queda guardado localmente.

Si "pip install sentence-transformers" falla (puede pasar con versiones
muy nuevas de Python, como la 3.14): avisame el error tal cual y vemos
una alternativa más liviana.
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
from sentence_transformers import SentenceTransformer

DB_PATH = "noticias.db"
MODELO = "paraphrase-multilingual-MiniLM-L12-v2"


def preparar_db(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS historias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo_representativo TEXT NOT NULL,
            creada TEXT NOT NULL
        )
        """
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(notas)")]
    if "historia_id" not in cols:
        con.execute("ALTER TABLE notas ADD COLUMN historia_id INTEGER")
    con.commit()


def cargar_notas_recientes(con, horas: int):
    desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
    return con.execute(
        "SELECT id, medio, titulo FROM notas WHERE fecha_recoleccion >= ? ORDER BY id",
        (desde,),
    ).fetchall()


def agrupar(filas, modelo, umbral: float):
    """Clustering incremental: cada nota se compara contra el centroide
    de cada historia ya armada; si la similitud supera el umbral entra
    ahí, si no arranca una historia nueva."""
    titulos = [t for _, _, t in filas]
    embeddings = modelo.encode(titulos, normalize_embeddings=True, show_progress_bar=True)

    historias = []  # cada elemento: {"indices": [...], "centroide": np.array}

    for i, emb in enumerate(embeddings):
        mejor_h, mejor_sim = None, -1.0
        for h in historias:
            sim = float(np.dot(emb, h["centroide"]))
            if sim > mejor_sim:
                mejor_h, mejor_sim = h, sim

        if mejor_h is not None and mejor_sim >= umbral:
            mejor_h["indices"].append(i)
            vecs = embeddings[mejor_h["indices"]]
            centroide = vecs.mean(axis=0)
            centroide /= np.linalg.norm(centroide)
            mejor_h["centroide"] = centroide
        else:
            historias.append({"indices": [i], "centroide": emb})

    return historias


def guardar(con, filas, historias):
    ahora = datetime.now(timezone.utc).isoformat()
    for h in historias:
        titulo_rep = filas[h["indices"][0]][2]
        cur = con.execute(
            "INSERT INTO historias (titulo_representativo, creada) VALUES (?, ?)",
            (titulo_rep, ahora),
        )
        historia_id = cur.lastrowid
        for idx in h["indices"]:
            nota_id = filas[idx][0]
            con.execute("UPDATE notas SET historia_id = ? WHERE id = ?", (historia_id, nota_id))
    con.commit()


def mostrar_resumen(filas, historias):
    multi = [h for h in historias if len({filas[i][1] for i in h["indices"]}) > 1]
    print(f"\n{len(historias)} historias armadas, {len(multi)} con más de un medio.\n")
    multi.sort(key=lambda h: -len(h["indices"]))
    for h in multi[:15]:
        medios = sorted({filas[i][1] for i in h["indices"]})
        print(f"--- {len(h['indices'])} notas, medios: {', '.join(medios)} ---")
        for idx in h["indices"]:
            print(f"  [{filas[idx][1]}] {filas[idx][2]}")
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horas", type=int, default=48, help="ventana de tiempo a agrupar")
    ap.add_argument("--umbral", type=float, default=0.73, help="similitud mínima para agrupar (0-1)")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    preparar_db(con)

    # cada corrida vuelve a agrupar desde cero, para poder probar distintos
    # umbrales sin ir acumulando historias viejas de pruebas anteriores
    con.execute("DELETE FROM historias")
    con.execute("UPDATE notas SET historia_id = NULL")
    con.commit()

    filas = cargar_notas_recientes(con, args.horas)
    if not filas:
        print("No hay notas en esa ventana de tiempo. Corré primero el recolector.")
        return

    print(f"Cargando modelo ({MODELO})... la primera vez puede tardar unos minutos.")
    modelo = SentenceTransformer(MODELO)

    print(f"Agrupando {len(filas)} notas con umbral {args.umbral}...")
    historias = agrupar(filas, modelo, args.umbral)

    guardar(con, filas, historias)
    mostrar_resumen(filas, historias)

    con.close()


if __name__ == "__main__":
    main()
