"""
Puente — generador de página (etapa 4 + indicador de inclinación política)

Lee las historias ya agrupadas (tabla `historias` en noticias.db, armada
por agrupar.py) y genera index.html: una página estática con cada
historia que tenga cobertura de 3 o más medios distintos, mostrando la
inclinación política promedio de esa cobertura (no la cantidad de
medios) y los medios que la cubrieron.

Requiere haber corrido antes recolector.py y agrupar.py.

Uso:
    python generar_pagina.py
    → genera docs/index.html
"""

import os
import sqlite3
from datetime import datetime, timezone

import yaml
from jinja2 import Environment, FileSystemLoader

DB_PATH = "noticias.db"
FEEDS_PATH = "feeds.yaml"
# docs/ es lo que GitHub Pages sirve como sitio
SALIDA = os.path.join("docs", "index.html")

MEDIOS_MINIMOS = 3  # una historia solo entra a la página si la cubrieron al menos estos medios distintos

# Escala de posición editorial: 1=Izquierda .. 5=Derecha
ETIQUETAS_POSICION = {
    1: "Izquierda",
    2: "Centro-izquierda",
    3: "Centro",
    4: "Centro-derecha",
    5: "Derecha",
}


def cargar_medios_confirmados():
    with open(FEEDS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    medios = data.get("confirmados", [])
    nombres = [m["medio"] for m in medios]
    posiciones = {}
    for m in medios:
        pos = m.get("posicion_editorial", 3)
        posiciones[m["medio"]] = pos if isinstance(pos, (int, float)) else 3
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
    return historias


def bucket_de_posicion(pos: float) -> str:
    if pos < 1.5:
        return ETIQUETAS_POSICION[1]
    if pos < 2.5:
        return ETIQUETAS_POSICION[2]
    if pos < 3.5:
        return ETIQUETAS_POSICION[3]
    if pos < 4.5:
        return ETIQUETAS_POSICION[4]
    return ETIQUETAS_POSICION[5]


def descripcion_promedio(promedio: float, n_coberturas: int) -> str:
    base = f"promedio de {n_coberturas} cobertura{'s' if n_coberturas != 1 else ''}"
    bucket = bucket_de_posicion(promedio)
    if bucket != "Centro":
        return base
    diff = abs(promedio - 3)
    if diff < 0.15:
        return base
    direccion = "la derecha" if promedio > 3 else "la izquierda"
    calificador = "leve inclinación a" if diff < 0.5 else "inclinación a"
    return f"{base}, {calificador} {direccion}"


def armar_indicador(notas, posiciones):
    """Strip-plot: agrupa las notas por la posición editorial de su medio,
    y calcula el promedio ponderado por cantidad de coberturas."""
    valores = [posiciones.get(n["medio"], 3) for n in notas]
    promedio = sum(valores) / len(valores)

    grupos = {}
    for n in notas:
        pos = posiciones.get(n["medio"], 3)
        grupos.setdefault(pos, []).append(n["medio"])

    puntos = []
    for pos, medios_en_pos in sorted(grupos.items()):
        n = len(medios_en_pos)
        puntos.append(
            {
                "x_pct": round((pos - 1) / 4 * 100, 2),
                "size": min(9 + 2 * (n - 1), 17),
                "titulo": ", ".join(medios_en_pos),
            }
        )

    return {
        "promedio": promedio,
        "promedio_x_pct": round((promedio - 1) / 4 * 100, 2),
        "etiqueta": bucket_de_posicion(promedio),
        "descripcion": descripcion_promedio(promedio, len(notas)),
        "puntos": puntos,
    }


def texto_medios_faltan(medios_faltan, tope=6):
    if not medios_faltan:
        return ""
    if len(medios_faltan) <= tope:
        return ", ".join(medios_faltan)
    resto = len(medios_faltan) - tope
    return ", ".join(medios_faltan[:tope]) + f" y {resto} medio{'s' if resto != 1 else ''} más"


def armar_contexto(historias, medios_confirmados, posiciones):
    resultado = []
    for notas in historias.values():
        medios_cubrieron = sorted({n["medio"] for n in notas})
        if len(medios_cubrieron) < MEDIOS_MINIMOS:
            continue
        medios_faltan = [m for m in medios_confirmados if m not in medios_cubrieron]
        resultado.append(
            {
                "titulo": notas[0]["titulo"],
                "notas": notas,
                "n_medios": len(medios_cubrieron),
                "medios_faltan": medios_faltan,
                "medios_faltan_texto": texto_medios_faltan(medios_faltan),
                "indicador": armar_indicador(notas, posiciones),
            }
        )
    # más medios primero (lo más compartido arriba); entre empatados, más notas primero
    resultado.sort(key=lambda h: (-h["n_medios"], -len(h["notas"])))
    return resultado


def main():
    medios_confirmados, posiciones = cargar_medios_confirmados()

    con = sqlite3.connect(DB_PATH)
    historias = cargar_historias(con)
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

    env = Environment(loader=FileSystemLoader("."))
    plantilla = env.get_template("plantilla.html")
    html = plantilla.render(
        historias=contexto_historias,
        total_medios=len(medios_confirmados),
        generado=datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        medios_minimos=MEDIOS_MINIMOS,
    )

    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Listo: {SALIDA} generado con {len(contexto_historias)} historias (de {len(historias)} agrupadas, filtrando por {MEDIOS_MINIMOS}+ medios).")


if __name__ == "__main__":
    main()
