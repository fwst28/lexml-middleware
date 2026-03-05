from fastapi import FastAPI, Query, Request, Path
from fastapi.openapi.utils import get_openapi
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup
import re
import unicodedata
import time
from datetime import date

PUBLIC_BASE_URL = "https://lexml-middleware.onrender.com"
LEXML_SEARCH = "https://www.lexml.gov.br/busca/search"

app = FastAPI(title="LexML Search Middleware", version="2.3.0")


# ✅ FORÇA "servers" no OpenAPI (resolve o erro do GPT Builder)
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description="Middleware para pesquisa no LexML (via HTML): jurisprudência, doutrina e legislação",
        routes=app.routes,
    )
    schema["servers"] = [{"url": PUBLIC_BASE_URL}]
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# ✅ LOG de qualquer request que chegue (Render -> Logs)
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
    return re.sub(r"\s+", " ", (s or "")).strip()


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
    # página 1 -> 1 ; página 2 -> 21 ; página 3 -> 41 ...
    return (page - 1) * page_size + 1


def _field(block: str, labels: list[str]) -> str | None:
    """
    Extrai valor para rótulos do LexML a partir do texto "visível" (soup.get_text).
    """
    stop = r"(?:Localidade|Autoridade|Título|Titulo|Data|Ementa|Resumo|Descrição|Descricao|URN|Assuntos|Autor|Autores|Autor\(es\)|Criador|Editor|Editora|Publicador|Fonte)"
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


def _authority_patterns(user_value: str) -> list[str]:
    v = _norm(user_value)
    if not v:
        return []
    if v in AUTH_ALIASES:
        return AUTH_ALIASES[v]

    pats = [v]
    if "superior tribunal de justica" in v:
        pats.append("stj")
    if "supremo tribunal federal" in v:
        pats.append("stf")
    if "tribunal superior do trabalho" in v:
        pats.append("tst")
    if "tribunal superior eleitoral" in v:
        pats.append("tse")
    if "tribunal de contas da uniao" in v:
        pats.append("tcu")
    return list(dict.fromkeys([p for p in pats if p]))


def _infer_legislation_number_year(title: str | None, urn: str | None) -> dict:
    """
    Tenta extrair número e ano de uma lei a partir do título ou URN.
    Exemplos comuns:
      - "Lei nº 13.709, de 2018"
      - "Lei Complementar nº 101, de 2000"
      - "Decreto nº 10.046, de 2019"
    """
    t = title or ""
    u = urn or ""

    # padrão: ... nº 13.709 ... 2018
    m = re.search(r"\b(n[ºo]\.?\s*)?(\d{1,5}(?:\.\d{1,3})?)\b.*?\b(19\d{2}|20\d{2})\b", t, flags=re.I)
    num = m.group(2) if m else None
    year = m.group(3) if m else None

    # tenta inferir espécie pelo título
    especie = None
    for k in ["Lei Complementar", "Lei", "Decreto", "Decreto-Lei", "Emenda Constitucional", "Medida Provisória", "Resolução", "Portaria", "Instrução Normativa"]:
        if re.search(rf"\b{re.escape(k)}\b", t, flags=re.I):
            especie = k
            break

    # fallback pelo URN (às vezes contém data/ano, mas não é garantido)
    if not year:
        m2 = re.search(r"\b(19\d{2}|20\d{2})\b", u)
        year = m2.group(1) if m2 else None

    return {"numero": num, "ano": year, "especie": especie}


def _abnt_reference(autor: str | None, titulo: str | None, local: str | None, editora: str | None, ano: str | None, url: str | None) -> dict:
    """
    Gera componentes para ABNT e uma referência "melhor esforço" (com campos ausentes omitidos).
    Observação: LexML pode não fornecer todos os campos (local/editora), então retornamos também os componentes.
    """
    a = _clean(autor) or None
    t = _clean(titulo) or None
    l = _clean(local) or None
    e = _clean(editora) or None
    y = _clean(ano) or None
    u = url

    parts = []
    if a:
        parts.append(a.upper())
    if t:
        parts.append(t)
    pub = []
    if l:
        pub.append(l)
    if e:
        pub.append(e)
    if y:
        pub.append(y)
    if pub:
        parts.append(": ".join(pub[:1]) + (", " + ", ".join(pub[1:]) if len(pub) > 1 else ""))

    if u:
        parts.append(f"Disponível em: {u}.")
    # acesso em (não dá pra saber o acesso real do usuário; colocamos data do servidor como default)
    parts.append(f"Acesso em: {date.today().strftime('%d/%m/%Y')}.")

    ref = " ".join([p for p in parts if p])

    return {
        "components": {
            "autor": a,
            "titulo": t,
            "local": l,
            "editora": e,
            "ano": y,
            "url": u,
        },
        "reference": ref
    }


def _search_lexml(q: str, start: int, limit: int, tipo_documento: str, patterns: list[str] | None, debug: int, mode: str):
    """
    mode:
      - 'juris': retorna campos típicos de jurisprudência (Autoridade, Ementa, etc.)
      - 'doutrina': retorna campos para ABNT + resumo
      - 'legis': retorna número e ano (quando possível)
    """
    params = {
        "keyword": q,
        "f1-tipoDocumento": tipo_documento,
    }
    startDoc = _startdoc_from_page(start, page_size=20)
    url = f"{LEXML_SEARCH}?{urlencode(params)};startDoc={startDoc}"

    # Limites para evitar timeout no Actions
    MAX_BLOCKS = 240
    MAX_SECONDS = 7.0
    t0 = time.time()

    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR,pt;q=0.9"},
            timeout=(8, 20),
        )
    except Exception as e:
        return {"context": {"q": q, "url": url, "tipo_documento": tipo_documento}, "error": {"type": "request_failed", "message": str(e)}, "results": []}

    if r.status_code >= 400:
        return {"context": {"q": q, "url": url, "tipo_documento": tipo_documento}, "error": {"type": "http_error", "status_code": r.status_code, "body_snippet": r.text[:400]}, "results": []}

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

        # filtro por tribunal (apenas usado na jurisprudência por tribunal)
        if patterns:
            autoridade_txt_for_filter = _field(block, ["Autoridade", "Autoridade Emitente"])
            hay = _norm(autoridade_txt_for_filter or "")
            if not hay:
                hay = _norm(block[:700])
            if not any(p in hay for p in patterns):
                continue

        # Campos comuns
        titulo = _field(block, ["Título", "Titulo"])
        data_txt = _field(block, ["Data"])  # pode vir completa
        localidade = _field(block, ["Localidade"])
        assuntos = _field(block, ["Assuntos"])

        # Resumo/Descrição (para doutrina)
        resumo = None
        if mode == "doutrina":
            # tenta "Resumo" ou "Descrição/Descricao" e fallback para um trecho do bloco
            resumo = _field(block, ["Resumo", "Descrição", "Descricao"])
            if not resumo:
                m_desc = re.search(r"\n\s*(?:Resumo|Descrição|Descricao)\s+(.*?)(?=\n\s*(?:URN|Assuntos)\s|\Z)", block, flags=re.S)
                resumo = _clean(m_desc.group(1)) if m_desc else None
            if not resumo:
                # fallback: pega um pedaço do bloco sem rótulos
                resumo = _clean(block[:600])

        # Jurisprudência: autoridade + ementa
        autoridade_txt = None
        ementa = None
        if mode == "juris":
            autoridade_txt = _field(block, ["Autoridade", "Autoridade Emitente"])
            m_ementa = re.search(r"\n\s*Ementa\s+(.*?)(?=\n\s*(?:URN|Assuntos)\s|\Z)", block, flags=re.S)
            ementa = _clean(m_ementa.group(1)) if m_ementa else None

        # Doutrina: autor/editor/editora/ano
        autor = None
        editora = None
        ano = None
        abnt = None
        if mode == "doutrina":
            autor = _field(block, ["Autor", "Autores", "Autor(es)", "Criador"])
            editora = _field(block, ["Editora", "Editor", "Publicador", "Fonte"])
            # tenta achar ano (de Data ou no título)
            m_year = re.search(r"\b(19\d{2}|20\d{2})\b", data_txt or "") or re.search(r"\b(19\d{2}|20\d{2})\b", titulo or "")
            ano = m_year.group(1) if m_year else None
            abnt = _abnt_reference(autor=autor, titulo=titulo, local=localidade, editora=editora, ano=ano, url=url_item)

        # Legislação: numero/ano
        numero_ano = None
        if mode == "legis":
            numero_ano = _infer_legislation_number_year(title=titulo, urn=urn)

        # Monta item de saída conforme mode
        if mode == "juris":
            item = {
                "title": titulo,
                "date": data_txt,
                "localidade": localidade,
                "autoridade": autoridade_txt,
                "ementa": ementa,
                "assuntos": assuntos,
                "urn": urn,
                "url": url_item,
            }
        elif mode == "doutrina":
            item = {
                "title": titulo,
                "date": data_txt,
                "autor": autor,
                "localidade": localidade,
                "editora": editora,
                "ano": ano,
                "assuntos": assuntos,
                "resumo": resumo,
                "abnt": abnt,     # inclui components + reference
                "urn": urn,
                "url": url_item,
            }
        else:  # legis
            item = {
                "title": titulo,
                "date": data_txt,
                "localidade": localidade,
                "assuntos": assuntos,
                "numero": numero_ano.get("numero") if numero_ano else None,
                "ano": numero_ano.get("ano") if numero_ano else None,
                "especie": numero_ano.get("especie") if numero_ano else None,
                "urn": urn,
                "url": url_item,
            }

        results.append(item)
        if len(results) >= limit:
            break

    resp = {"context": {"q": q, "url": url, "tipo_documento": tipo_documento, "mode": mode}, "results": results}

    if (scanned >= MAX_BLOCKS or stopped_by_time) and len(results) < limit:
        resp["warning"] = {
            "message": "Interrompi varredura para evitar timeout. Tente start menor, limit menor ou refine a query.",
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
            "stopped_by_time": stopped_by_time,
            "elapsed_seconds": round(time.time() - t0, 3),
        }

    return resp


# =========================
# ✅ Jurisprudência
# =========================

@app.get("/lexml/jurisprudencia")
def buscar_jurisprudencia(
    q: str = Query(...),
    start: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0),
):
    return _search_lexml(q=q, start=start, limit=limit, tipo_documento="Jurisprudência", patterns=None, debug=debug, mode="juris")


@app.get("/lexml/jurisprudencia/{tribunal}")
def buscar_jurisprudencia_por_tribunal(
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
    resp = _search_lexml(q=q, start=start, limit=limit, tipo_documento="Jurisprudência", patterns=patterns, debug=debug, mode="juris")
    resp["context"]["tribunal"] = tribunal
    return resp


# =========================
# ✅ Doutrina
# =========================

@app.get("/lexml/doutrina")
def buscar_doutrina(
    q: str = Query(...),
    start: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0),
):
    # Doutrina não filtra por tribunal aqui (o LexML mistura autores/obras)
    return _search_lexml(q=q, start=start, limit=limit, tipo_documento="Doutrina", patterns=None, debug=debug, mode="doutrina")


# =========================
# ✅ Legislação
# =========================

@app.get("/lexml/legislacao")
def buscar_legislacao(
    q: str = Query(...),
    start: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    debug: int = Query(0),
):
    return _search_lexml(q=q, start=start, limit=limit, tipo_documento="Legislação", patterns=None, debug=debug, mode="legis")
