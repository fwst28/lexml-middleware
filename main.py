from fastapi import FastAPI, Query
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re

# ✅ IMPORTANTE: aumente a versão para você confirmar no /docs que o Render atualizou
app = FastAPI(title="LexML Search Middleware", version="2.0.0")

LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _startdoc_from_page(page: int, page_size: int = 20) -> int:
    # página 1 -> 1 ; página 2 -> 21 ; página 3 -> 41 ...
    return (page - 1) * page_size + 1


def _field(block: str, labels: list[str]) -> str | None:
    """
    Extrai valor para um dos rótulos, tolerando variações como Título/Titulo.
    """
    stop = r"(?:Localidade|Autoridade|Título|Titulo|Data|Ementa|URN|Assuntos)"
    for label in labels:
        if label.lower() == "localidade":
            # Ex.: "1 Localidade  Distrito FederalAdicionar"
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


@app.get("/health")
def health():
    return {"ok": True, "service": app.title, "version": app.version}


@app.get("/lexml/jurisprudencia")
def buscar(
    q: str = Query(..., description="Termos (ex: prisão preventiva garantia da ordem pública)"),
    autoridade: str | None = Query(None, description="Filtro textual (ex: STJ, STF, Superior Tribunal de Justiça)"),
    start: int = Query(1, ge=1, description="Página (1,2,3...)"),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0, description="debug=1 retorna diagnóstico quando results vier vazio"),
):
    # ✅ Montagem correta: keyword é só o texto, e filtros vão em parâmetros separados
    params = {
        "keyword": q,
        "f1-tipoDocumento": "Jurisprudência",
    }

    startDoc = _startdoc_from_page(start, page_size=20)
    url = f"{LEXML_SEARCH}?{urlencode(params)};startDoc={startDoc}"

    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR,pt;q=0.9"},
            timeout=(10, 30),  # (connect, read)
        )
    except Exception as e:
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "error": {"type": "request_failed", "message": str(e)},
            "results": [],
        }

    if r.status_code >= 400:
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "error": {"type": "http_error", "status_code": r.status_code, "body_snippet": r.text[:600]},
            "results": [],
        }

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n", strip=True)
    except Exception as e:
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "error": {"type": "parse_failed", "message": str(e), "html_snippet": r.text[:800]},
            "results": [],
        }

    # ✅ Split robusto por itens e detecção por URN (marcador mais estável)
    blocks = re.split(r"\n(?=\d+\s+)", "\n" + text)
    urn_pat = re.compile(r"\bURN\s+(urn:lex:[^\s]+)")

    results = []

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

        m_ementa = re.search(r"\n\s*Ementa\s+(.*?)(?=\n\s*(?:URN|Assuntos)\s|\Z)", block, flags=re.S)
        ementa = _clean(m_ementa.group(1)) if m_ementa else None

        # ✅ Filtro por autoridade (pós-processamento). Aceita STJ/STF como alias.
        if autoridade:
            a = autoridade.strip().lower()
            at = (autoridade_txt or "").lower()

            ok = a in at
            if not ok:
                if a == "stj":
                    ok = "superior tribunal de justiça" in at
                elif a == "stf":
                    ok = "supremo tribunal federal" in at

            if not ok:
                continue

        results.append({
            "title": titulo,
            "date": data,
            "localidade": localidade,
            "autoridade": autoridade_txt,
            "ementa": ementa,
            "assuntos": assuntos,
            "urn": urn,
            "url": url_item,
        })

        if len(results) >= limit:
            break

    if not results and debug == 1:
        return {
            "context": {"q": q, "autoridade": autoridade, "url": url},
            "debug": {
                "urn_count_in_page_text": len(urn_pat.findall(text)),
                "text_preview": text[:1400],
                "blocks_count": len(blocks),
                "note": "Se urn_count_in_page_text > 0, a página tem resultados e ajustamos labels/regex."
            },
            "results": [],
        }

    return {"context": {"q": q, "autoridade": autoridade, "url": url}, "results": results}
