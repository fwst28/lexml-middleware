from fastapi import FastAPI, Query
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re

app = FastAPI(
    title="LexML Search Middleware",
    version="1.1.0",
    servers=[{"url": "https://lexml-middleware.onrender.com"}],
)

LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

@app.get("/lexml/jurisprudencia")
def buscar(
    q: str = Query(..., description="Termos (ex: ICMS PIS COFINS base cálculo)"),
    autoridade: str | None = Query(None, description="Órgão por extenso (ex: Supremo Tribunal Federal)"),
    start: int = Query(1, ge=1, description="Página (1,2,3...)"),
    limit: int = Query(10, ge=1, le=50),
):
    keyword = q

    keyword_with_filters = f"{keyword};f1-tipoDocumento=Jurisprudência"

    if autoridade:
        keyword_with_filters += f";f1-autoridadeEmitente={autoridade}"

    params = {
        "keyword": keyword_with_filters,
        "page": str(start),
    }

    url = f"{LEXML_SEARCH}?{urlencode(params)}"

    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if r.status_code >= 400:
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "error": {"status_code": r.status_code, "body_snippet": r.text[:300]},
            "results": []
        }

    soup = BeautifulSoup(r.text, "html.parser")

    urn_links = [a.get("href") for a in soup.find_all("a", href=True) if "/urn/" in a.get("href")]
    urn_links = [("https://www.lexml.gov.br" + h) if h.startswith("/") else h for h in urn_links]

    text = soup.get_text("\n", strip=True)
    parts = re.split(r"\n\d+\s+Adicionar\n", "\n" + text)

    results = []
    link_idx = 0

    for chunk in parts[1:]:
        lines = [l.strip() for l in chunk.split("\n") if l.strip()]

        def pick(label: str):
            for i, ln in enumerate(lines):
                if ln.startswith(label):
                    val = ln[len(label):].strip(" :")
                    if val:
                        return _clean(val)
                    if i + 1 < len(lines):
                        return _clean(lines[i+1])
            return None

        tipo = pick("Tipo")
        autoridade_txt = pick("Autoridade") or pick("Autoridade Emitente")
        titulo = pick("Título")
        data = pick("Data")
        ementa = pick("Ementa")
        assuntos = pick("Assuntos")

        url_item = urn_links[link_idx] if link_idx < len(urn_links) else None
        link_idx += 1

        results.append({
            "title": titulo,
            "date": data,
            "tipo": tipo,
            "autoridade": autoridade_txt,
            "ementa": ementa,
            "assuntos": assuntos,
            "url": url_item,
        })

        if len(results) >= limit:
            break

    return {
        "context": {"q": q, "autoridade": autoridade, "url": url},
        "results": results
    }
