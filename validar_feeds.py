"""
Ground News Argentina — validador de feeds (etapa 1)

Lee feeds.yaml y confirma que cada feed_url devuelve un RSS válido con
notas recientes. Pensado para correr en un entorno con acceso normal a
internet (tu compu, un servidor, o el runner de GitHub Actions que se
arme en la etapa 5) — no dentro de este espacio de trabajo en la nube,
que tiene el acceso a internet restringido a un listado de dominios y
por eso esta etapa se validó a mano, sitio por sitio.

Uso:
    pip install feedparser requests pyyaml
    python validar_feeds.py
"""

import sys
import yaml
import feedparser
import requests

# Los sitios argentinos suelen bloquear pedidos sin User-Agent de navegador.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}

TIMEOUT = 15


def validar_feed(medio: str, url: str) -> dict:
    resultado = {"medio": medio, "url": url, "ok": False, "n_items": 0, "detalle": ""}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        resultado["detalle"] = f"error de conexión: {e}"
        return resultado

    if resp.status_code != 200:
        resultado["detalle"] = f"status {resp.status_code}"
        return resultado

    parsed = feedparser.parse(resp.content)
    n = len(parsed.entries)
    resultado["n_items"] = n

    if parsed.bozo and n == 0:
        resultado["detalle"] = f"XML mal formado: {parsed.bozo_exception}"
        return resultado

    if n == 0:
        resultado["detalle"] = "el feed no tiene items"
        return resultado

    resultado["ok"] = True
    resultado["detalle"] = f"{n} items, primero: {parsed.entries[0].get('title', '(sin título)')}"
    return resultado


def main():
    with open("feeds.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    medios = data.get("confirmados", [])
    if not medios:
        print("No hay medios en 'confirmados' dentro de feeds.yaml")
        sys.exit(1)

    print(f"Validando {len(medios)} feeds...\n")
    fallos = 0
    for m in medios:
        r = validar_feed(m["medio"], m["feed_url"])
        estado = "OK " if r["ok"] else "FALLA"
        print(f"[{estado}] {r['medio']}: {r['detalle']}")
        if not r["ok"]:
            fallos += 1

    print(f"\n{len(medios) - fallos}/{len(medios)} feeds funcionando.")
    if fallos:
        print("Revisar los que fallaron antes de pasar a la etapa 2 (recolector).")


if __name__ == "__main__":
    main()
