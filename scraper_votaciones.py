"""
Extractor incremental de votaciones nominales del Congreso argentino.

- Diputados: descarga las actas PDF de votaciones.hcdn.gob.ar/pdf/acta/<id>
  (IDs correlativos) y extrae encabezado, totales y voto de cada diputado.
- Senado:    descarga el detalle de cada acta en senado.gob.ar y lee la tabla
  de votos (estructura a verificar en la primera corrida, ver SENADO_* abajo).

Salida (carpeta ./datos):
  votaciones.csv  -> una fila por votación
  votos.csv       -> una fila por legislador y votación
  estado.json     -> último ID procesado por cámara (para correr de forma incremental)

Uso:
  pip install requests pdfplumber pandas lxml
  python scraper_votaciones.py                 # incremental, ambas cámaras
  python scraper_votaciones.py --camara diputados --desde 5800
"""

import argparse
import csv
import io
import json
import re
import time
from pathlib import Path

import pdfplumber
import requests

DATOS = Path("datos")
ESTADO = DATOS / "estado.json"
PAUSA = 1.5            # segundos entre pedidos, para no sobrecargar los servidores
MAX_FALLOS = 15        # IDs vacíos seguidos antes de asumir que no hay más actas
MAX_ACTAS = 300        # tope por corrida (la carga histórica se completa en varias corridas)

HCDN_PDF = "https://votaciones.hcdn.gob.ar/pdf/acta/{id}"
SENADO_DETALLE = "https://www.senado.gob.ar/votaciones/detalleActa/{id}"  # verificar

HEADERS = {"User-Agent": "Mozilla/5.0 (investigacion academica; contacto: completar)"}

VOTOS = ("AFIRMATIVO", "NEGATIVO", "ABSTENCION", "ABSTENCIÓN", "AUSENTE", "PRESIDENTE")
DISTRITOS = [
    "Buenos Aires", "C.A.B.A.", "Cdad. Aut. Bs. As.", "Cap. Federal", "Catamarca", "Chaco",
    "Chubut", "Córdoba", "Corrientes", "Entre Ríos", "Formosa", "Jujuy", "La Pampa",
    "La Rioja", "Mendoza", "Misiones", "Neuquén", "Río Negro", "Rio Negro", "Salta",
    "San Juan", "San Luis", "Santa Cruz", "Santa Fe", "Santiago del Estero",
    "Tierra del Fuego", "Tucumán",
]
RE_FILA = re.compile(
    r"^(?P<nombre>[A-ZÁÉÍÓÚÑÜ'\"\-\. ]+,\s.+?)\s+(?P<resto>.+?)\s+"
    r"(?P<voto>" + "|".join(VOTOS) + r")$"
)


# ---------------------------------------------------------------- utilidades
def cargar_estado():
    if ESTADO.exists():
        return json.loads(ESTADO.read_text())
    return {"diputados": 0, "senado": 0}


def guardar_estado(estado):
    ESTADO.write_text(json.dumps(estado, indent=2))


def agregar_csv(ruta, filas, campos):
    nuevo = not ruta.exists()
    with ruta.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        if nuevo:
            w.writeheader()
        w.writerows(filas)


def separar_bloque_distrito(resto):
    """El PDF trae 'Bloque Distrito' juntos; el distrito se reconoce por la lista."""
    for d in sorted(DISTRITOS, key=len, reverse=True):
        if resto.endswith(" " + d) or resto == d:
            return resto[: -len(d)].strip(), d
    return resto, ""


def num(patron, texto):
    m = re.search(patron, texto)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- Diputados
def parsear_acta_diputados(pdf_bytes, acta_id):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texto = "\n".join(p.extract_text() or "" for p in pdf.pages)

    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    # Une nombres partidos en dos líneas (ej. "ARANCIBIA RODRIGUEZ, ALBERTO" + "GUSTAVO")
    unidas = []
    for l in lineas:
        if unidas and not unidas[-1].endswith(VOTOS) and "," in unidas[-1] \
                and not RE_FILA.match(unidas[-1]) and l.isupper() and "," not in l:
            unidas[-1] += " " + l
        else:
            unidas.append(l)

    reunion = next((l for l in lineas if "Período" in l or "Periodo" in l), "")
    idx = lineas.index(reunion) if reunion in lineas else -1
    titulo = lineas[idx + 1] if 0 <= idx < len(lineas) - 1 else ""
    fecha = re.search(r"(\d{2}/\d{2}/\d{4})", texto)
    hora = re.search(r"(\d{1,2}:\d{2})", texto)

    votacion = {
        "camara": "diputados",
        "acta_id": acta_id,
        "fecha": fecha.group(1) if fecha else "",
        "hora": hora.group(1) if hora else "",
        "reunion": reunion,
        "titulo": titulo,
        "presentes": num(r"Presentes\s+\d+\s+\d+\s+(\d+)", texto),
        "ausentes": num(r"Ausentes\s+(\d+)", texto),
        "afirmativos": num(r"Afirmativos\s+(?:\d+\s+){2,3}?(\d+)\s*$", texto) or num(r"Afirmativos\s+(\d+)", texto),
        "negativos": num(r"Negativos\s+(?:\d+\s+){2,3}?(\d+)\s*$", texto) or num(r"Negativos\s+(\d+)", texto),
        "abstenciones": num(r"Abstenciones\s+(?:\d+\s+){1,2}?(\d+)\s*$", texto) or num(r"Abstenciones\s+(\d+)", texto),
        "resultado": (re.search(r"Resultado de la Votaci[oó]n:?\s*(\w+)", texto) or [None, ""])[1],
        "fuente": HCDN_PDF.format(id=acta_id),
    }

    votos = votos_por_columnas(pdf_bytes, acta_id)
    if not votos:  # respaldo: actas con otro diseño
        for l in unidas:
            m = RE_FILA.match(l)
            if not m:
                continue
            bloque, distrito = separar_bloque_distrito(m["resto"])
            votos.append({
                "camara": "diputados", "acta_id": acta_id,
                "legislador": m["nombre"].strip(), "bloque": bloque, "distrito": distrito,
                "voto": m["voto"].replace("ABSTENCIÓN", "ABSTENCION"),
            })
    return votacion, votos


def votos_por_columnas(pdf_bytes, acta_id):
    """Ubica las columnas por la posición horizontal de los títulos 'Bloque' y
    'Distrito'/'Provincia' y asigna cada palabra a su columna. Resuelve nombres
    que el PDF parte en dos líneas."""
    votos = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            xb = next((w["x0"] for w in words if w["text"].startswith("Bloque")), None)
            xd = next((w["x0"] for w in words if w["text"].startswith(("Distrito", "Provincia"))), None)
            if xb is None or xd is None or xd <= xb:
                continue
            y_hdr = next(w["top"] for w in words if w["text"].startswith("Bloque"))
            lineas = {}
            for w in words:
                if w["top"] <= y_hdr + 2:
                    continue
                lineas.setdefault(round(w["top"] / 2), []).append(w)
            filas, sueltos = [], []  # sueltos = renglones con solo parte de un nombre
            for clave in sorted(lineas):
                ws = sorted(lineas[clave], key=lambda w: w["x0"])
                top = ws[0]["top"]
                voto = next((w["text"] for w in ws if w["text"] in VOTOS), None)
                if voto is None:
                    if all(w["x0"] < xb - 2 for w in ws):
                        sueltos.append((top, ws))
                    continue
                filas.append({"top": top, "voto": voto, "nombre": [(top, [w for w in ws if w["x0"] < xb - 2])],
                              "bloque": [w["text"] for w in ws if xb - 2 <= w["x0"] < xd - 2],
                              "distrito": [w["text"] for w in ws if w["x0"] >= xd - 2 and w["text"] != voto]})
            # cada fragmento de nombre va a la fila de voto más cercana en vertical
            for top, ws in sueltos:
                cerca = min(filas, key=lambda f: abs(f["top"] - top), default=None)
                if cerca and abs(cerca["top"] - top) < 20:
                    cerca["nombre"].append((top, ws))
            for f in filas:
                nombre = " ".join(w["text"] for _, ws in sorted(f["nombre"], key=lambda t: t[0]) for w in ws)
                if not nombre:
                    continue
                votos.append({
                    "camara": "diputados", "acta_id": acta_id,
                    "legislador": nombre, "bloque": " ".join(f["bloque"]),
                    "distrito": " ".join(f["distrito"]),
                    "voto": f["voto"].replace("ABSTENCIÓN", "ABSTENCION"),
                })
    return votos


def bajar_diputados(desde, sesion):
    actual, fallos, total, ultimo_ok = desde, 0, 0, desde
    while fallos < MAX_FALLOS and total < MAX_ACTAS:
        actual += 1
        r = sesion.get(HCDN_PDF.format(id=actual), timeout=60)
        time.sleep(PAUSA)
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            fallos += 1
            continue
        fallos = 0
        votacion, votos = parsear_acta_diputados(r.content, actual)
        if not votos:
            print(f"  [aviso] acta {actual}: sin votos reconocidos, revisar formato")
        agregar_csv(DATOS / "votaciones.csv", [votacion], list(votacion))
        if votos:
            agregar_csv(DATOS / "votos.csv", votos, list(votos[0]))
        total += 1
        print(f"  Diputados acta {actual}: {votacion['titulo'][:60]} ({len(votos)} votos)")
        ultimo_ok = actual
    return ultimo_ok, total


# ---------------------------------------------------------------- Senado
def bajar_senado(desde, sesion):
    """Lee la tabla HTML de cada acta. Verificar en el navegador la URL de detalle
    (SENADO_DETALLE) y el nombre de las columnas antes de la primera corrida larga."""
    import pandas as pd

    actual, fallos, total, ultimo_ok = desde, 0, 0, desde
    while fallos < MAX_FALLOS and total < MAX_ACTAS:
        actual += 1
        url = SENADO_DETALLE.format(id=actual)
        r = sesion.get(url, timeout=60)
        time.sleep(PAUSA)
        if r.status_code != 200:
            fallos += 1
            continue
        try:
            tablas = pd.read_html(io.StringIO(r.text))
        except ValueError:
            fallos += 1
            continue
        tabla = next((t for t in tablas if any("Voto" in str(c) for c in t.columns)), None)
        if tabla is None:
            fallos += 1
            continue
        fallos = 0
        cols = {str(c).lower(): c for c in tabla.columns}
        col = lambda *claves: next((cols[k] for k in cols if any(x in k for x in claves)), None)
        c_nom, c_blq, c_prov, c_voto = col("senador", "nombre"), col("bloque"), col("provincia"), col("voto")

        texto = re.sub(r"<[^>]+>", " ", r.text)
        fecha = re.search(r"(\d{2}/\d{2}/\d{4})", texto)
        titulo = re.search(r"<title>(.*?)</title>", r.text, re.S)
        votacion = {
            "camara": "senado", "acta_id": actual, "fecha": fecha.group(1) if fecha else "",
            "hora": "", "reunion": "", "titulo": titulo.group(1).strip() if titulo else "",
            "presentes": None, "ausentes": None, "afirmativos": None, "negativos": None,
            "abstenciones": None, "resultado": "", "fuente": url,
        }
        votos = [{
            "camara": "senado", "acta_id": actual,
            "legislador": str(f[c_nom]).strip() if c_nom is not None else "",
            "bloque": str(f[c_blq]).strip() if c_blq is not None else "",
            "distrito": str(f[c_prov]).strip() if c_prov is not None else "",
            "voto": str(f[c_voto]).strip().upper() if c_voto is not None else "",
        } for _, f in tabla.iterrows()]
        for v in ("AFIRMATIVO", "NEGATIVO", "ABSTENCION", "AUSENTE"):
            votacion[{"AFIRMATIVO": "afirmativos", "NEGATIVO": "negativos",
                      "ABSTENCION": "abstenciones", "AUSENTE": "ausentes"}[v]] = \
                sum(x["voto"].startswith(v[:6]) for x in votos)
        agregar_csv(DATOS / "votaciones.csv", [votacion], list(votacion))
        agregar_csv(DATOS / "votos.csv", votos, list(votos[0]))
        total += 1
        print(f"  Senado acta {actual}: {len(votos)} votos")
        ultimo_ok = actual
    return ultimo_ok, total


# ---------------------------------------------------------------- main
def main():
    global MAX_ACTAS
    ap = argparse.ArgumentParser()
    ap.add_argument("--camara", choices=["diputados", "senado", "ambas"], default="ambas")
    ap.add_argument("--desde", type=int, help="forzar ID inicial (exclusivo)")
    ap.add_argument("--max", type=int, default=MAX_ACTAS, help="máximo de actas por cámara en esta corrida")
    args = ap.parse_args()
    MAX_ACTAS = args.max

    DATOS.mkdir(exist_ok=True)
    estado = cargar_estado()
    sesion = requests.Session()
    sesion.headers.update(HEADERS)

    if args.camara in ("diputados", "ambas"):
        desde = args.desde if args.desde is not None else estado["diputados"]
        estado["diputados"], n = bajar_diputados(desde, sesion)
        print(f"Diputados: {n} actas nuevas (último ID {estado['diputados']})")
        guardar_estado(estado)

    if args.camara in ("senado", "ambas"):
        desde = args.desde if args.desde is not None else estado["senado"]
        estado["senado"], n = bajar_senado(desde, sesion)
        print(f"Senado: {n} actas nuevas (último ID {estado['senado']})")
        guardar_estado(estado)


if __name__ == "__main__":
    main()
