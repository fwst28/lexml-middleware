# main.py
from __future__ import annotations

from typing import List, Optional, Any, Dict
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlencode


# ----------------------------
# Models (geram OpenAPI correto)
# ----------------------------

class LexmlContext(BaseModel):
    q: str
    autoridade: Optional[str] = None
    url: str


class LexmlJurisprudenciaItem(BaseModel):
    title: str
    date: Optional[str] = None
    localidade: Optional[str] = None
    autoridade: Optional[str] = None
    ementa: Optional[str] = None
    assuntos: Optional[str] = None
    urn: Optional[str] = None
    url: str


class LexmlDiagnostico(BaseModel):
    status: Optional[int] = None
    hint: Optional[str] = None
    upstream_url: Optional[str] = None
    raw_excerpt: Optional[str] = None


class LexmlJurisprudenciaResponse(BaseModel):
    context: LexmlContext
    results: List[LexmlJurisprudenciaItem]
    diagnostico: Optional[LexmlDiagnostico] = None


# ----------------------------
# FastAPI app (com servers p/ Actions)
# ----------------------------

app = FastAPI(
    title="LexML Search Middleware",
    version="1.1.4",
    servers=[{"url": "https://lexml-middleware.onrender.com"}],  # <- importante p/ Actions
)


@app.get("/", tags=["health"])
def root():
    return {"ok": True, "service": "lexml-middleware"}


@app.get("/health", tags=["health"])
def health():
    return {"ok": True}


# ----------------------------
# Helper: monta URL do LexML
# ----------------------------

def build_lexml_url(q: str, start: int, limit: int) -> str:
    """
    LexML usa /busca/search e aceita keyword e startDoc.
    - startDoc parece ser 1-indexado (start=1 -> startDoc=1).
    """
    start_doc = (start - 1) * limit + 1
    # f1-tipoDocumento = Jurisprudência (atenção ao acento)
    params = {
        "keyword": q,
        "f1-tipoDocumento": "Jurisprudência",
        "startDoc": str(start_doc),
    }
    return "https://www.lexml.gov.br/busca/search?" + urlencode(params)


def normalize_text(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = " ".join(s.split())
    return s.strip() or None


# ----------------------------
# Scraper simples (pode exigir ajuste se o HTML mudar)
# ----------------------------

async def fetch_and_parse_lexml(upstream_url: str, limit: int) -> Dict[str, Any]:
    """
    Retorna dict com keys:
      - results: List[LexmlJurisprudenciaItem-like dicts]
      - raw_excerpt: trecho do HTML (p/ debug)
    """
    timeout = httpx.Timeout(25.0, connect=10.0)
    headers = {
        "User-Agent": "lexml-middleware/1.1.4 (+https://lexml-middleware.onrender.com)"
    }

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        r = await client.get(upstream_url)
        status = r.status_code
        r.raise_for_status()

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Estratégia: tenta achar blocos de resultado por links /urn/
    # O LexML costuma ter links para "https://www.lexml.gov.br/urn/..."
    urn_links = soup.select('a[href*="://www.lexml.gov.br/urn/"], a[href^="/urn/"]')

    items: List[Dict[str, Any]] = []
    seen_urls = set()

    for a in urn_links:
        href = a.get("href") or ""
        if href.startswith("/"):
            href = "https://www.lexml.gov.br" + href

        if "lexml.gov.br/urn/" not in href:
            continue

        title = normalize_text(a.get_text())
        if not title:
            continue

        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Tenta buscar infos no container mais próximo
        container = a.find_parent(["div", "li", "article"]) or a.parent

        # Heurísticas para date/autoridade/ementa (podem variar)
        text_block = normalize_text(container.get_text(" ", strip=True)) if container else None

        # date: tenta achar padrão dd/mm/aaaa no texto do container
        date = None
        if text_block:
            import re
            m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", text_block)
            if m:
                date = m.group(1)

        # autoridade: tenta capturar algo como "Superior Tribunal de Justiça..."
        autoridade = None
        if text_block:
            # procura por trechos comuns
            for needle in [
                "Supremo Tribunal Federal",
                "Superior Tribunal de Justiça",
                "Tribunal Regional",
                "Tribunal de Justiça",
                "Turma",
                "Seção",
                "Câmara",
            ]:
                if needle.lower() in text_block.lower():
                    # pega um recorte pequeno
                    idx = text_block.lower().find(needle.lower())
                    autoridade = normalize_text(text_block[idx: idx + 120])
                    break

        # ementa: tenta pegar um parágrafo maior no container
        ementa = None
        if container:
            p = container.find("p")
            if p:
                ementa = normalize_text(p.get_text(" ", strip=True))

        # urn: às vezes dá pra extrair da URL
        urn = None
        if "lexml.gov.br/urn/" in href:
            # o "urn:" vem no path após /urn/
            urn_part = href.split("/urn/")[-1]
            # LexML costuma incluir "urn:lex:..." no path
            if urn_part.startswith("urn:"):
                urn = urn_part

        items.append(
            {
                "title": title,
                "date": date,
                "localidade": None,
                "autoridade": autoridade,
                "ementa": ementa,
                "assuntos": None,
                "urn": urn,
                "url": href,
            }
        )

        if len(items) >= limit:
            break

    # Excerpt para debug (primeiros 1500 chars)
    raw_excerpt = html[:1500]

    return {"results": items, "raw_excerpt": raw_excerpt, "status": status}


# ----------------------------
# Endpoint principal
# ----------------------------

@app.get(
    "/lexml/jurisprudencia",
    summary="Buscar",
    operation_id="buscar_lexml_jurisprudencia_get",
    response_model=LexmlJurisprudenciaResponse,  # <- ESSENCIAL para o OpenAPI ficar bom
    tags=["lexml"],
)
async def buscar_lexml_jurisprudencia(
    q: str = Query(..., description="Termos (ex: improbidade administrativa dolo STF)"),
    autoridade: Optional[str] = Query(None, description="Filtro textual (ex: Supremo Tribunal Federal)"),
    start: int = Query(1, ge=1, description="Página (1,2,3...)"),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0, description="Use debug=1 para retornar diagnóstico quando results vier vazio"),
) -> LexmlJurisprudenciaResponse:
    upstream_url = build_lexml_url(q=q, start=start, limit=limit)

    context = LexmlContext(q=q, autoridade=autoridade, url=upstream_url)

    diagnostico: Optional[LexmlDiagnostico] = None
    results: List[LexmlJurisprudenciaItem] = []

    try:
        parsed = await fetch_and_parse_lexml(upstream_url=upstream_url, limit=limit)
        raw_items = parsed["results"]

        # Filtro por autoridade (textual), se informado
        if autoridade:
            auth_lower = autoridade.lower()
            raw_items = [
                it for it in raw_items
                if (it.get("autoridade") or "").lower().find(auth_lower) != -1
            ]

        results = [LexmlJurisprudenciaItem(**it) for it in raw_items]

        # Se veio vazio e debug=1, devolve diagnóstico útil
        if debug == 1 and len(results) == 0:
            diagnostico = LexmlDiagnostico(
                status=parsed.get("status"),
                hint="Nenhum item parseado. Pode ser que o LexML tenha mudado o HTML ou que o filtro 'autoridade' esteja restritivo.",
                upstream_url=upstream_url,
                raw_excerpt=parsed.get("raw_excerpt"),
            )

    except httpx.HTTPStatusError as e:
        # erro HTTP do LexML
        if debug == 1:
            diagnostico = LexmlDiagnostico(
                status=e.response.status_code if e.response else None,
                hint=f"Erro HTTP ao consultar LexML: {str(e)}",
                upstream_url=upstream_url,
                raw_excerpt=(e.response.text[:1500] if e.response is not None else None),
            )
    except Exception as e:
        if debug == 1:
            diagnostico = LexmlDiagnostico(
                status=None,
                hint=f"Erro inesperado: {repr(e)}",
                upstream_url=upstream_url,
            )

    return LexmlJurisprudenciaResponse(context=context, results=results, diagnostico=diagnostico)
