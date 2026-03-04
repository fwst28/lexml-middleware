from fastapi import FastAPI, Query
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re

app = FastAPI(title="LexML Search Middleware", version="1.1.4")

LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _startdoc_from_page(page: int, page_size: int = 20) -> int:
    return (page - 1) * page_size + 1


def _field(block: str, labels: list[str]) -> str | None:
    """
    Extrai valor para um dos rótulos (com tolerância a variações como Título/Titulo).
    """
    # Normaliza fim do campo no próximo label conhecido ou no final do bloco
    stop = r"(?:Localidade|Autoridade|Título|Titulo|Data|Ementa|URN|Assuntos)"
    for label in labels:
        # Caso especial: Localidade às vezes vem como "1 Localidade ...Adicionar"
        if label.lower() == "localidade":
            m = re.search(r"\n\s*\d+\s+Localidade\s+(.*?)(?:Adicionar|\n)", block, flags=re.S)
            if m:
                return _clean(m.group(1))
            continue

        m = re.search(
            rf"\n\s*{re.escape(label)}\s+(.*?)(?=\n\s*{stop}\s|\Z)",
            block,
            flags=re.S,
        )
        if m:
            return _clean(m.group(1))

    return None


@app.get("/lexml/jurisprudencia")
def buscar(
    q: str = Query(..., description="Termos (ex: improbidade administrativa dolo STF)"),
    autoridade: str | None = Query(None, description="Filtro textual (ex: Supremo Tribunal Federal)"),
    start: int = Query(1, ge=1, description="Página (1,2,3...)"),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0, description="Use debug=1 para retornar diagnóstico quando results vier vazio"),
):
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
    text = soup.get_text("\n", strip=True)

    # ✅ Split mais robusto: cada item costuma começar com "<número> ..."
    # Depois filtramos só os blocos que contenham URN (o marcador mais estável).
    blocks = re.split(r"\n(?=\d+\s+)", "\n" + text)

    results = []
    urn_pat = re.compile(r"\bURN\s+(urn:lex:[^\s]+)")

    for block in blocks:
        m_urn = urn_pat.search(block)
        if not m_urn:
            continue

        urn = m_urn.group(1)
        url_item = f"https://www.lexml.gov.br/urn/{urn}"

        localidade = _field(block, ["Localidade"])
        autoridade_txt = _field(block, ["Autoridade", "Autoridade Emitente"])
        titulo = _field(block, ["Título", "Titulo"])
        data = _field(block, ["Data"])
        assuntos = _field(block, ["Assuntos"])

        # Ementa pode ser grande: pega do rótulo "Ementa" até URN/Assuntos/fim
        m_ementa = re.search(r"\n\s*Ementa\s+(.*?)(?=\n\s*(?:URN|Assuntos)\s|\Z)", block, flags=re.S)
        ementa = _clean(m_ementa.group(1)) if m_ementa else None

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

        # filtro por autoridade (pós-processamento)
        if autoridade:
            if not (autoridade_txt and autoridade.lower() in autoridade_txt.lower()):
                continue

        results.append(item)
        if len(results) >= limit:
            break

    # Diagnóstico quando vier vazio (para ajustar rápido se o LexML mudar de layout)
    if not results and debug == 1:
        urn_count = len(urn_pat.findall(text))
        preview = text[:1200]
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "debug": {
                "text_preview": preview,
                "urn_count_in_page_text": urn_count,
                "blocks_count": len(blocks),
                "note": "Se urn_count_in_page_text > 0 e results vazio, o layout/labels mudaram e ajustamos a regex."
            },
            "results": []
        }

    return {
        "context": {"q": q, "autoridade": autoridade, "url": url},
        "results": results
    }
