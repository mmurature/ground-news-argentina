"""
Puente — validador de feeds (etapa 1 + ampliación de cobertura)

Lee feeds.yaml y confirma que cada feed_url devuelve un RSS válido con
notas recientes. Pensado para correr en un entorno con acceso normal a
internet (tu compu, un servidor, o el runner de GitHub Actions) — no
dentro del espacio de trabajo en la nube donde se arma este archivo,
que tiene el acceso a internet restringido a un listado de dominios.

Uso:
    pip install feedparser requests pyyaml
    python validar_feeds.py                  # valida "candidatos" (default)
    python validar_feeds.py --grupo confirmados
    python validar_feeds.py --grupo todos
"""

import argparse
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


def validar_grupo(nombre: str, medios: list) -> int:
    """Valida una lista de medios, imprime resultados, devuelve la cantidad de fallos."""
    if not medios:
        print(f"(sin medios en '{nombre}')\n")
        return 0
    print(f"--- {nombre} ({len(medios)}) ---")
    fallos = 0
    for m in medios:
        r = validar_feed(m["medio"], m["feed_url"])
        estado = "OK " if r["ok"] else "FALLA"
        print(f"[{estado}] {r['medio']}: {r['detalle']}")
        if not r["ok"]:
            fallos += 1
    print(f"{len(medios) - fallos}/{len(medios)} funcionando.\n")
    return fallos


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--grupo",
        choices=["candidatos", "confirmados", "todos"],
        default="candidatos",
        help="qué lista de feeds.yaml validar (default: candidatos)",
    )
    args = parser.parse_args()

    with open("feeds.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    grupos = []
    if args.grupo in ("candidatos", "todos"):
        grupos.append(("candidatos", data.get("candidatos", [])))
    if args.grupo in ("confirmados", "todos"):
        grupos.append(("confirmados", data.get("confirmados", [])))

    if not any(medios for _, medios in grupos):
        print(f"No hay medios en '{args.grupo}' dentro de feeds.yaml")
        sys.exit(1)

    total_fallos = 0
    total_medios = 0
    for nombre, medios in grupos:
        total_fallos += validar_grupo(nombre, medios)
        total_medios += len(medios)

    print(f"Total: {total_medios - total_fallos}/{total_medios} feeds funcionando.")
    if total_fallos:
        print("Los que fallaron se pueden reintentar más tarde, o descartar si el sitio bloquea el acceso automático.")
    print("\nLos que anden en 'candidatos': moverlos a mano a 'confirmados' en feeds.yaml (con su posicion_editorial si se sabe) para que el recolector los empiece a usar.")


if __name__ == "__main__":
    main()
