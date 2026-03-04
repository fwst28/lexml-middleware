from fastapi import FastAPI, Query
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re

app = FastAPI(title="LexML Search Middleware", version="1.1.2")

LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _startdoc_from_page(page: int, page_size: int = 20) -> int:
    # LexML usa startDoc=1 na primeira página e incrementa ~20 por página (ex.: 21 na pág. 2).
    return (page - 1) * page_size + 1


def _grab_block_field(block: str, label: str) -> str | None:
    """
    Extrai o conteúdo após um rótulo em um bloco de texto do LexML.
    Ex.: "Autoridade  Superior Tribunal de Justiça. 1ª Seção"
    """
    m = re.search(rf"\n\s*{re.escape(label)}\s+(.*?)(?=\n\s*(?:Localidade|Autoridade|Título|Data|Ementa|URN|Assuntos)\s|\Z)",
                  block, flags=re.S)
    return _clean(m.group(1)) if m else None


@app.get("/lexml/jurisprudencia")
def buscar(
    q: str = Query(..., description="Termos (ex: ICMS PIS COFINS base cálculo)"),
    autoridade: str | None = Query(None, description="Filtro textual (ex: Supremo Tribunal Federal)"),
    start: int = Query(1, ge=1, description="Página (1,2,3...)"),
    limit: int = Query(10, ge=1, le=50),
):
    # 1) Monta URL de busca do LexML (web) da forma estável:
    # keyword=<termos>&f1-tipoDocumento=Jurisprudência
    params = {
        "keyword": q,
        "f1-tipoDocumento": "Jurisprudência",
    }

    startDoc = _startdoc_from_page(start, page_size=20)
    url = f"{LEXML_SEARCH}?{urlencode(params)};startDoc={startDoc}"

    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if r.status_code >= 400:
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "error": {"status_code": r.status_code, "body_snippet": r.text[:400]},
            "results": []
        }

    soup = BeautifulSoup(r.text, "html.parser")

    # 2) Extrai o texto “visível” da página
    text = soup.get_text("\n", strip=True)

    # 3) Quebra por itens: cada resultado começa com "<número> Localidade"
    blocks = re.split(r"\n(?=\d+\s+Localidade\s)", "\n" + text)

    results = []

    for block in blocks:
        if not re.search(r"\n\d+\s+Localidade\s", block):
            continue

        localidade = _grab_block_field(block, "Localidade")
        autoridade_txt = _grab_block_field(block, "Autoridade")
        titulo = _grab_block_field(block, "Título")
        data = _grab_block_field(block, "Data")

        # Ementa pode ser longa: pega do rótulo "Ementa" até "URN" ou "Assuntos"
        m_ementa = re.search(r"\n\s*Ementa\s+(.*?)(?=\n\s*(?:URN|Assuntos)\s|\Z)", block, flags=re.S)
        ementa = _clean(m_ementa.group(1)) if m_ementa else None

        # URN (formato típico urn:lex:...)
        m_urn = re.search(r"\n\s*URN\s+(urn:lex:[^\s]+)", block)
        urn = m_urn.group(1) if m_urn else None

        m_assuntos = re.search(r"\n\s*Assuntos\s+(.*?)(?=\n\s*\d+\s+Localidade\s|\Z)", block, flags=re.S)
        assuntos = _clean(m_assuntos.group(1)) if m_assuntos else None

        url_item = f"https://www.lexml.gov.br/urn/{urn}" if urn else None

        item = {
            "title": titulo,
            "date": data,
            "localidade": localidade,
            "autoridade": autoridade_txt,
            "ementa": ementa,
            "assuntos": assuntos,
            "urn": urn,
            "url": url_item,
        }

        # 4) Filtro por autoridade (pós-processamento, mais estável)
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
