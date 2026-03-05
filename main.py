from fastapi import FastAPI, Query, Request
from fastapi.openapi.utils import get_openapi
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re

PUBLIC_BASE_URL = "https://lexml-middleware.onrender.com"
LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"

app = FastAPI(title="LexML Search Middleware", version="2.0.2")


# ✅ FORÇA "servers" no OpenAPI (resolve o erro do GPT Builder)
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description="Middleware para pesquisa de jurisprudência no LexML (via HTML)",
        routes=app.routes,
    )

    schema["servers"] = [{"url": PUBLIC_BASE_URL}]
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# ✅ LOG de qualquer request que chegue (veja em Render -> Logs)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"[REQ] {request.method} {request.url.path}?{request.url.query}")
    response = await call_next(request)
    print(f"[RES] {request.method} {request.url.path} -> {response.status_code}")
    return response


@app.get("/ping")
def ping():
    return {"ok": True, "ping": "pong"}


@app.get("/health")
def health():
    return {"ok": True, "service": app.title, "version": app.version}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _startdoc_from_page(page: int, page_size: int = 20) -> int:
    return (page - 1) * page_size + 1


def _field(block: str, labels: list[str]) -> str | None:
    stop = r"(?:Localidade|Autoridade|Título|Titulo|Data|Ementa|URN|Assuntos)"
    for label in labels:
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
    q: str = Query(...),
    autoridade: str | None = Query(None),
    start: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0),
):
    # ✅ Montagem correta: keyword é só texto; filtro vai em parâmetro separado
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
            timeout=(10, 30),
        )
    except Exception as e:
        return {"context": {"q": q, "autoridade": autoridade, "url": url},
                "error": {"type": "request_failed", "message": str(e)},
                "results": []}

    if r.status_code >= 400:
        return {"context": {"q": q, "autoridade": autoridade, "url": url},
                "error": {"type": "http_error", "status_code": r.status_code, "body_snippet": r.text[:600]},
                "results": []}

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n", strip=True)

    blocks = re.split(r"\n(?=\d+\s+)", "\n" + text)
    urn_pat = re.compile(r"\bURN\s+(urn:lex:[^\s]+)")
    results = []

    for block in blocks:
        m_urn = urn_pat.search(block)
        if not m_urn:
            continue

        urn = m_urn.group(1)
        url_item = f"https://www.lexml.gov.br/urn/{urn}"

        autoridade_txt = _field(block, ["Autoridade", "Autoridade Emitente"])
        titulo = _field(block, ["Título", "Titulo"])
        data = _field(block, ["Data"])
        assuntos = _field(block, ["Assuntos"])

        m_ementa = re.search(r"\n\s*Ementa\s+(.*?)(?=\n\s*(?:URN|Assuntos)\s|\Z)", block, flags=re.S)
        ementa = _clean(m_ementa.group(1)) if m_ementa else None

        # ✅ Filtro por autoridade (pós-processamento), aceitando STJ/STF
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
                "text_preview": text[:1200],
                "blocks_count": len(blocks),
            },
            "results": [],
        }

    return {"context": {"q": q, "autoridade": autoridade, "url": url}, "results": results}
