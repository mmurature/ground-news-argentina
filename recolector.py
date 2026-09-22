"""
Ground News Argentina — recolector (etapa 2)

Lee feeds.yaml (los medios en "confirmados"), junta las notas de cada feed
y las guarda en noticias.db (SQLite). Correrlo de nuevo no duplica notas:
cada nota se identifica por su link, que es único en la tabla.

Uso:
    python recolector.py
"""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml

DB_PATH = "noticias.db"
FEEDS_PATH = "feeds.yaml"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}
TIMEOUT = 15
DIAS_A_CONSERVAR = 7  # notas más viejas que esto se borran, para no hacer
                       # crecer la base sin límite (el agrupador solo mira
                       # las últimas 48hs de todos modos)


def conectar_db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS notas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medio TEXT NOT NULL,
            titulo TEXT NOT NULL,
            resumen TEXT,
            link TEXT NOT NULL UNIQUE,
            fecha_publicacion TEXT,
            fecha_recoleccion TEXT NOT NULL
        )
        """
    )
    con.commit()
    return con


def cargar_feeds():
    with open(FEEDS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("confirmados", [])


def podar_notas_viejas(con, dias: int) -> int:
    limite = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    cur = con.execute("DELETE FROM notas WHERE fecha_recoleccion < ?", (limite,))
    con.commit()
    return cur.rowcount


def fecha_publicacion(entry):
    for campo in ("published_parsed", "updated_parsed"):
        val = entry.get(campo)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc).isoformat()
    return None


def recolectar_medio(con, medio: str, url: str):
    """Devuelve (notas_vistas, notas_nuevas)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ERROR al bajar el feed: {e}")
        return (0, 0)

    parsed = feedparser.parse(resp.content)
    vistas = len(parsed.entries)
    nuevas = 0
    ahora = datetime.now(timezone.utc).isoformat()

    for entry in parsed.entries:
        link = entry.get("link")
        titulo = entry.get("title")
        if not link or not titulo:
            continue
        resumen = entry.get("summary", "")
        cur = con.execute(
            "INSERT OR IGNORE INTO notas "
            "(medio, titulo, resumen, link, fecha_publicacion, fecha_recoleccion) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (medio, titulo, resumen, link, fecha_publicacion(entry), ahora),
        )
        if cur.rowcount:
            nuevas += 1

    con.commit()
    return (vistas, nuevas)


def main():
    medios = cargar_feeds()
    if not medios:
        print("No hay medios en 'confirmados' dentro de feeds.yaml")
        sys.exit(1)

    con = conectar_db()

    borradas = podar_notas_viejas(con, DIAS_A_CONSERVAR)
    if borradas:
        print(f"Se borraron {borradas} notas de más de {DIAS_A_CONSERVAR} días.\n")

    total_vistas = total_nuevas = 0

    for m in medios:
        print(f"{m['medio']}...")
        vistas, nuevas = recolectar_medio(con, m["medio"], m["feed_url"])
        print(f"  {vistas} notas en el feed, {nuevas} nuevas guardadas")
        total_vistas += vistas
        total_nuevas += nuevas

    total_en_db = con.execute("SELECT COUNT(*) FROM notas").fetchone()[0]
    con.close()

    print(f"\nTotal: {total_vistas} notas vistas, {total_nuevas} nuevas.")
    print(f"La base ahora tiene {total_en_db} notas guardadas en total.")


if __name__ == "__main__":
    main()
