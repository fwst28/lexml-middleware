from fastapi import FastAPI, Query
import requests
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

app = FastAPI(title="LexML SRU Middleware", version="1.0.0")

LEXML_SRU = "https://www.lexml.gov.br/busca/SRU"

def _txt(el):
    return (el.text or "").strip() if el is not None and el.text else None

@app.get("/lexml/jurisprudencia")
def buscar(
    q: str = Query(..., description="Termos (ex: ICMS PIS COFINS base cálculo)"),
    autoridade: str | None = Query(None, description="Ex: Supremo Tribunal Federal"),
    start: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
):
    # Monta CQL simples a partir dos termos
    termos = [t for t in q.split() if t.strip()]
    cql = " and ".join([f'dc.description all "{t}"' for t in termos[:8]]) if termos else 'dc.description all ""'
    cql += ' and facet-tipoDocumento="Jurisprudência"'
    if autoridade:
        cql += f' and autoridade all "{autoridade}"'

    params = {
        "operation": "searchRetrieve",
        "version": "1.1",
        "query": cql,
        "startRecord": start,
        "maximumRecords": limit,
        "recordPacking": "xml",
    }
    url = f"{LEXML_SRU}?{urlencode(params)}"

    r = requests.get(url, headers={"Accept": "application/xml"}, timeout=20)
    r.raise_for_status()

    root = ET.fromstring(r.text)

    results = []
    for rec in root.findall(".//{*}record"):
        data = rec.find(".//{*}recordData")
        if data is None:
            continue

        title = _txt(data.find(".//{*}title"))
        date = _txt(data.find(".//{*}date"))
        descs = [d.text.strip() for d in data.findall(".//{*}description") if d is not None and d.text]

        ids = [i.text.strip() for i in data.findall(".//{*}identifier") if i is not None and i.text]
        urn = next((i for i in ids if i.startswith("urn:")), None)
        link = f"https://www.lexml.gov.br/urn/{urn}" if urn else None

        results.append({
            "title": title,
            "date": date,
            "descriptions": descs,
            "urn": urn,
            "url": link,
        })

    return {
        "context": {"q": q, "autoridade": autoridade, "cql": cql, "start": start, "limit": limit},
        "results": results,
    }