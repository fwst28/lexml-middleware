from fastapi import FastAPI, Query
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re

app = FastAPI(title="LexML Search Middleware", version="1.1.1")

LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def _startdoc_from_page(page: int, page_size: int = 20) -> int:
    # LexML usa startDoc=1 na primeira página e incrementa ~20 por página (ex.: 21 na pág. 2). :contentReference[oaicite:1]{index=1}
    return (page - 1) * page_size + 1

@app.get("/lexml/jurisprudencia")
def buscar(
    q: str = Query(..., description="Termos (ex: ICMS PIS COFINS base cálculo)"),
    autoridade: str | None = Query(None, description="Filtro textual (ex: Supremo Tribunal Federal)"),
    start: int = Query(1, ge=1, description="Página (1,2,3...)"),
    limit: int = Query(10, ge=1, le=50),
):
    # 1) Monta URL de busca do LexML (web) do jeito que o site usa:
    # keyword=<termos>&f1-tipoDocumento=Jurisprudência
    params = {
        "keyword": q,
        "f1-tipoDocumento": "Jurisprudência",
    }

    # Paginação via startDoc (o LexML usa ;startDoc=21 etc.) :contentReference[oaicite:2]{index=2}
    startDoc = _startdoc_from_page(start, page_size=20)
    url = f"{LEXML_SEARCH}?{urlencode(params)};startDoc={startDoc}"

    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30
    )
    if r.status_code >= 400:
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "error": {"status_code": r.status_code, "body_snippet": r.text[:300]},
            "results": []
        }

    soup = BeautifulSoup(r.text, "html.parser")

    # Links de URN (resultados)
    urn_links = [a.get("href") for a in soup.find_all("a", href=True) if "/urn/" in a.get("href")]
    urn_links = [("https://www.lexml.gov.br" + h) if h.startswith("/") else h for h in urn_links]

    # Quebra por itens ("Adicionar" aparece nos blocos)
    text = soup.get_text("\n", strip=True)
    parts = re.split(r"\n\d+\s+Localidade\s+.*?Adicionar\n", "\n" + text)

    results = []
    link_idx = 0

    def pick(lines, label: str):
        for i, ln in enumerate(lines):
            if ln.startswith(label):
                val = ln[len(label):].strip(" :")
                if val:
                    return _clean(val)
                if i + 1 < len(lines):
                    return _clean(lines[i + 1])
        return None

    for chunk in parts[1:]:
        lines = [l.strip() for l in chunk.split("\n") if l.strip()]

        tipo = pick(lines, "Tipo")
        autoridade_txt = pick(lines, "Autoridade") or pick(lines, "Autoridade Emitente")
        titulo = pick(lines, "Título")
        data = pick(lines, "Data")
        ementa = pick(lines, "Ementa")
        assuntos = pick(lines, "Assuntos")
        urn = pick(lines, "URN")

        # URL (preferir URN link; se não existir, monta pelo URN textual)
        url_item = urn_links[link_idx] if link_idx < len(urn_links) else None
        link_idx += 1
        if not url_item and urn:
            url_item = f"https://www.lexml.gov.br/urn/{urn}"

        item = {
            "title": titulo,
            "date": data,
            "tipo": tipo,
            "autoridade": autoridade_txt,
            "ementa": ementa,
            "assuntos": assuntos,
            "urn": urn,
            "url": url_item,
        }

        # 2) Filtro por autoridade (pós-processamento, mais estável)
        if autoridade:
            if not (autoridade_txt and autoridade.lower() in autoridade_txt.lower()):
                continue

        results.append(item)
        if len(results) >= limit:
            break

    return {
        "context": {"q": q, "autoridade": autoridade, "url": url},
        "results": results
    }
