"""
Ground News Argentina — generador de página (etapa 4)

Lee las historias ya agrupadas (tabla `historias` en noticias.db, armada
por agrupar.py) y genera index.html: una página estática, mobile-first,
con cada historia, los medios que la cubrieron (con su propio título y
link) y los medios confirmados que no dijeron nada.

Requiere haber corrido antes recolector.py y agrupar.py.

Uso:
    python generar_pagina.py
    → genera index.html en esta misma carpeta
"""

import os
import sqlite3
from datetime import datetime, timezone

import yaml
from jinja2 import Environment, FileSystemLoader

DB_PATH = "noticias.db"
FEEDS_PATH = "feeds.yaml"
# docs/ es lo que GitHub Pages va a servir como sitio
SALIDA = os.path.join("docs", "index.html")


def cargar_medios_confirmados():
    with open(FEEDS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [m["medio"] for m in data.get("confirmados", [])]


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
    return historias


def armar_contexto(historias, medios_confirmados):
    resultado = []
    for notas in historias.values():
        medios_cubrieron = sorted({n["medio"] for n in notas})
        medios_faltan = [m for m in medios_confirmados if m not in medios_cubrieron]
        resultado.append(
            {
                "titulo": notas[0]["titulo"],
                "notas": notas,
                "n_medios": len(medios_cubrieron),
                "medios_faltan": medios_faltan,
            }
        )
    # más medios primero (lo más compartido arriba); entre empatados, más notas primero
    resultado.sort(key=lambda h: (-h["n_medios"], -len(h["notas"])))
    return resultado


def main():
    medios_confirmados = cargar_medios_confirmados()

    con = sqlite3.connect(DB_PATH)
    historias = cargar_historias(con)
    con.close()

    if not historias:
        print("No hay historias agrupadas todavía. Corré primero recolector.py y agrupar.py.")
        return

    contexto_historias = armar_contexto(historias, medios_confirmados)

    env = Environment(loader=FileSystemLoader("."))
    plantilla = env.get_template("plantilla.html")
    html = plantilla.render(
        historias=contexto_historias,
        total_medios=len(medios_confirmados),
        generado=datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
    )

    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Listo: {SALIDA} generado con {len(contexto_historias)} historias.")


if __name__ == "__main__":
    main()
