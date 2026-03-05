from fastapi import FastAPI, Query, Request, Path
from fastapi.openapi.utils import get_openapi
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re
import unicodedata
import time

PUBLIC_BASE_URL = "https://lexml-middleware.onrender.com"
LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"

app = FastAPI(title="LexML Search Middleware", version="2.2.0")


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


def _norm(s: str) -> str:
    """lower + remove acentos + normaliza espaços"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _clean(s).lower()


AUTH_ALIASES = {
    "stj": ["superior tribunal de justica", "stj"],
    "stf": ["supremo tribunal federal", "stf"],
    "tst": ["tribunal superior do trabalho", "tst"],
    "tse": ["tribunal superior eleitoral", "tse"],
    "tcu": ["tribunal de contas da uniao", "tcu"],
}


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


def _search_lexml(q: str, start: int, limit: int, patterns: list[str] | None, debug: int):
    """
    Busca no LexML (HTML) e retorna resultados.
    patterns: lista de padrões normalizados para filtrar por tribunal (pós-processamento).
    """
    params = {
        "keyword": q,
        "f1-tipoDocumento": "Jurisprudência",
    }
    startDoc = _startdoc_from_page(start, page_size=20)
    url = f"{LEXML_SEARCH}?{urlencode(params)};startDoc={startDoc}"

    # Limites para evitar timeout no Actions
    MAX_BLOCKS = 220
    MAX_SECONDS = 6.0
    t0 = time.time()

    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR,pt;q=0.9"},
            timeout=(8, 18),
        )
    except Exception as e:
        return {"context": {"q": q, "url": url}, "error": {"type": "request_failed", "message": str(e)}, "results": []}

    if r.status_code >= 400:
        return {"context": {"q": q, "url": url}, "error": {"type": "http_error", "status_code": r.status_code}, "results": []}

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n", strip=True)

    blocks = re.split(r"\n(?=\d+\s+)", "\n" + text)
    urn_pat = re.compile(r"\bURN\s+(urn:lex:[^\s]+)")

    results = []
    scanned = 0
    stopped_by_time = False

    for block in blocks:
        scanned += 1
        if scanned > MAX_BLOCKS:
            break
        if (time.time() - t0) > MAX_SECONDS:
            stopped_by_time = True
            break

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

        # Filtro por tribunal (robusto): tenta em Autoridade; se vazio, tenta no bloco
        if patterns:
            hay = _norm(autoridade_txt or "")
            if not hay:
                hay = _norm(block[:700])
            if not any(p in hay for p in patterns):
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

    resp = {"context": {"q": q, "url": url}, "results": results}

    if patterns and (scanned >= MAX_BLOCKS or stopped_by_time) and len(results) < limit:
        resp["warning"] = {
            "message": "Filtro por tribunal pode exigir varrer muitos resultados; limitei a varredura para evitar timeout.",
            "scanned_blocks": scanned,
            "max_blocks": MAX_BLOCKS,
            "stopped_by_time": stopped_by_time,
            "elapsed_seconds": round(time.time() - t0, 3),
        }

    if not results and debug == 1:
        resp["debug"] = {
            "urn_count_in_page_text": len(urn_pat.findall(text)),
            "text_preview": text[:1200],
            "blocks_count": len(blocks),
            "scanned_blocks": scanned,
            "patterns": patterns,
            "stopped_by_time": stopped_by_time,
            "elapsed_seconds": round(time.time() - t0, 3),
        }

    return resp


# ✅ Endpoint SEM filtro (sempre funcionou bem)
@app.get("/lexml/jurisprudencia")
def buscar_geral(
    q: str = Query(...),
    start: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0),
):
    return _search_lexml(q=q, start=start, limit=limit, patterns=None, debug=debug)


# ✅ Endpoint COM filtro por tribunal via PATH (mais estável no Actions)
@app.get("/lexml/jurisprudencia/{tribunal}")
def buscar_por_tribunal(
    tribunal: str = Path(..., description="Use: stj, stf, tst, tse, tcu"),
    q: str = Query(...),
    start: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0),
):
    t = _norm(tribunal)
    patterns = AUTH_ALIASES.get(t)
    if not patterns:
        return {
            "context": {"q": q, "tribunal": tribunal},
            "error": {"type": "invalid_tribunal", "message": "Use um destes: stj, stf, tst, tse, tcu"},
            "results": []
        }
    resp = _search_lexml(q=q, start=start, limit=limit, patterns=patterns, debug=debug)
    resp["context"]["tribunal"] = tribunal
    return resp
