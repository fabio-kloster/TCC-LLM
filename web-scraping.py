"""Coleta inicial de notícias oficiais, sem LLM ou serviços externos."""

import argparse
import json
import logging
import math
import re
import time
import unicodedata
from collections import deque
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


BASE = "https://prefeitura.rio/"
USER_AGENT = "TCCLLM-Coletor/0.1 (pesquisa academica; Python requests)"
LOG = logging.getLogger("coletor")
MONTHS = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


class AccessError(Exception):
    """Falha HTTP, redirecionamento externo ou regra de acesso."""


def normalize(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def matches_artist(artist, title, text):
    # Bordas evitam que 'Ana' coincida com 'Copacabana'.
    pattern = r"(?<!\w)" + re.escape(normalize(artist)) + r"(?!\w)"
    return any(re.search(pattern, normalize(value)) for value in (title, text))


def official_url(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    return (parts.scheme in {"https", "http"}
            and (host == "prefeitura.rio" or host.endswith(".prefeitura.rio"))
            and not parts.username and not parts.password
            and parts.port in {None, 80, 443})


def normalize_url(url, base=BASE):
    parts = urlsplit(urljoin(base, url))
    # Remove somente rastreadores conhecidos; preserva consultas funcionais.
    query = parse_qs(parts.query, keep_blank_values=True)
    query = {k: v for k, v in query.items()
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}}
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path or "/", urlencode(sorted(query.items()), doseq=True), ""))


def parse_date(value):
    """Interpreta datas explícitas; nunca adiciona hora ou fuso ausente."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value).isoformat()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
                        r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", value):
            precision = "auto" if re.search(r"[T ]\d{2}:\d{2}:\d{2}", value) else "minutes"
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(timespec=precision)
        numeric = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})"
                               r"(?:\s*-?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?", value)
        if numeric:
            day, month, year, hour, minute, second = numeric.groups()
            if hour is None:
                return date(int(year), int(month), int(day)).isoformat()
            return datetime(int(year), int(month), int(day), int(hour),
                            int(minute), int(second or 0)).isoformat(timespec="seconds" if second else "minutes")
        written = re.fullmatch(r"(\d{1,2}) de ([a-z]+) de (\d{4})", normalize(value))
        if written:
            day, month, year = written.groups()
            return date(int(year), MONTHS[month], int(day)).isoformat()
    except (ValueError, KeyError):
        pass
    return None


class HttpClient:
    """Acessos sequenciais com robots por origem, timeout e retries limitados."""

    def __init__(self, interval=1.0, timeout=30.0, attempts=3, user_agent=USER_AGENT):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,text/plain"})
        self.user_agent = user_agent
        self.interval = interval
        self.timeout = timeout
        self.attempts = attempts
        self.last_access = 0.0
        self.robots = {}

    def close(self):
        self.session.close()

    def _request(self, url, interval):
        for attempt in range(self.attempts):
            time.sleep(max(0.0, interval - (time.monotonic() - self.last_access)))
            self.last_access = time.monotonic()
            try:
                response = self.session.get(url, timeout=self.timeout, allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt + 1 == self.attempts:
                    raise AccessError(f"falha de rede após {self.attempts} tentativas: {exc}") from exc
                delay = 2 ** attempt
            except requests.RequestException as exc:
                raise AccessError(f"falha de requisição: {exc}") from exc
            else:
                if response.status_code not in {429, 500, 502, 503, 504}:
                    return response
                if attempt + 1 == self.attempts:
                    raise AccessError(f"HTTP {response.status_code} após {self.attempts} tentativas")
                delay = 2 ** attempt
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after)
                                                - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError, OverflowError):
                            pass
                if not math.isfinite(delay) or delay > 60:
                    raise AccessError(f"servidor exige espera longa ({retry_after}); repetir a coleta depois")
            LOG.warning("Nova tentativa para %s em %.1fs", url, delay)
            time.sleep(delay)
        raise AccessError("tentativas esgotadas")

    def _rules(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            try:
                response = self._fetch(robots_url, check_robots=False)
                parser = RobotFileParser(robots_url)
                parser.parse(response.text.splitlines())
            except AccessError as exc:
                # Sem regras verificáveis, não faz coleta nessa origem.
                self.robots[origin] = str(exc)
            else:
                self.robots[origin] = parser
        rules = self.robots[origin]
        if isinstance(rules, str):
            raise AccessError(f"robots.txt não verificável em {origin}: {rules}")
        return rules

    def _fetch(self, url, check_robots=True):
        for _ in range(6):
            if not official_url(url):
                raise AccessError(f"URL fora do domínio permitido: {url}")
            interval = self.interval
            if check_robots:
                rules = self._rules(url)
                if not rules.can_fetch(self.user_agent, url):
                    raise AccessError(f"acesso proibido por robots.txt: {url}")
                interval = max(interval, rules.crawl_delay(self.user_agent) or 0)
                rate = rules.request_rate(self.user_agent)
                if rate:
                    interval = max(interval, rate.seconds / rate.requests)
                if interval > 60:
                    raise AccessError("robots.txt exige intervalo superior a 60s; coleta interrompida nessa origem")
            response = self._request(url, interval)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise AccessError("redirecionamento sem Location")
                url = normalize_url(location, url)
                continue
            if response.status_code >= 400:
                raise AccessError(f"HTTP {response.status_code}: {url}")
            if response.status_code != 200:
                raise AccessError(f"resposta inesperada HTTP {response.status_code}: {url}")
            return response
        raise AccessError("excesso de redirecionamentos")

    def get_html(self, url):
        response = self._fetch(url)
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise AccessError(f"resposta não HTML: {response.headers.get('Content-Type')}")
        # O HTML observado declara UTF-8; evita o default ISO-8859-1 do requests.
        encoding = requests.utils.get_encodings_from_content(response.text[:4000])
        if encoding:
            response.encoding = encoding[0]
        elif not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding
        return response.text, normalize_url(response.url)


def search_links(html, url):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for heading in soup.select("article.section-content-categories h3, article .entry-title"):
        anchor = heading.find_parent("a") or heading.find("a")
        if anchor and anchor.get("href"):
            links.append(normalize_url(anchor["href"], url))
    pagination = set()
    for anchor in soup.select("a[href]"):
        target = normalize_url(anchor["href"], url)
        parts = urlsplit(target)
        query = parse_qs(parts.query)
        # A própria busca publica links /page/N/?s=... em outros subdomínios.
        if (query.get("s") == parse_qs(urlsplit(url).query).get("s")
                and (re.fullmatch(r"/page/\d+/", parts.path) or "paged" in query)
                and official_url(target)):
            pagination.add(target)
    empty = any(phrase in normalize(soup.get_text(" ", strip=True))
                for phrase in ("nenhum resultado", "nenhum registro", "nada encontrado",
                               "nao encontramos", "nenhuma noticia"))
    return links, sorted(pagination), empty


def discover(client, artist, max_search_pages, records, partial_reasons):
    queue = deque([BASE + "?" + urlencode({"s": artist})])
    visited, seen_urls, signatures = set(), set(), set()
    urls = []
    while queue:
        url = queue.popleft()
        if url in visited:
            continue
        if len(visited) >= max_search_pages:
            partial_reasons.append("limite de páginas de busca atingido")
            records.append({"url": url, "status": "pendente", "etapa": "descoberta",
                            "motivo": "limite de páginas de busca atingido"})
            break
        visited.add(url)
        LOG.info("Busca: %s", url)
        try:
            html, final_url = client.get_html(url)
            candidates, pages, empty = search_links(html, final_url)
        except (AccessError, ValueError) as exc:
            records.append({"url": url, "status": "falha", "etapa": "descoberta", "motivo": str(exc)})
            partial_reasons.append("falha na descoberta")
            LOG.error("Falha de descoberta: %s", exc)
            continue
        if not candidates and not empty:
            records.append({"url": url, "status": "pendente", "etapa": "descoberta",
                            "motivo": "layout de busca sem resultados nem indicação explícita de busca vazia"})
            partial_reasons.append("layout de busca não reconhecido")
        signature = tuple(sorted(set(candidates)))
        if candidates and signature in signatures:
            records.append({"url": url, "status": "pendente", "etapa": "descoberta",
                            "motivo": "paginação repetiu os mesmos resultados"})
            partial_reasons.append("paginação repetida")
            continue
        signatures.add(signature)
        for candidate in candidates:
            if candidate in seen_urls:
                continue
            seen_urls.add(candidate)
            if official_url(candidate):
                urls.append(candidate)
            else:
                records.append({"url": candidate, "status": "descartado", "etapa": "descoberta",
                                "motivo": "URL fora de prefeitura.rio e seus subdomínios"})
        queue.extend(page for page in pages if page not in visited)
    return urls, len(visited)


def article_schema(soup):
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if any(t in {"Article", "NewsArticle", "BlogPosting"} for t in types):
                yield value
            if "@graph" in value:
                yield from walk(value["@graph"])

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue


def metadata_date(soup, schema, kind):
    key = "article:published_time" if kind == "publication" else "article:modified_time"
    meta = soup.find("meta", attrs={"property": key})
    if meta and meta.get("content", "").strip():
        return meta["content"].strip()
    schema_key = "datePublished" if kind == "publication" else "dateModified"
    for item in schema:
        if item.get(schema_key):
            return item[schema_key]
    selector = "time.published, [itemprop=datePublished]" if kind == "publication" else "time.updated, [itemprop=dateModified]"
    node = soup.select_one(selector)
    if node:
        return node.get("datetime") or node.get("content") or node.get_text(" ", strip=True)
    header = soup.select_one(".title-post")
    if header:
        label = "Publicado em" if kind == "publication" else "Atualizado em"
        match = re.search(label + r"\s+(\d{1,2}/\d{1,2}/\d{4}(?:\s*-\s*\d{1,2}:\d{2}(?::\d{2})?)?)",
                          header.get_text(" ", strip=True), re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract(html, url):
    soup = BeautifulSoup(html, "html.parser")
    schema = list(article_schema(soup))
    og = soup.find("meta", attrs={"property": "og:type"})
    body_classes = soup.body.get("class", []) if soup.body else []
    is_news = bool(schema or (og and og.get("content") == "article") or "single-post" in body_classes)
    title = soup.select_one(".title-post h1, h1.entry-title, main h1")
    title = title.get_text(" ", strip=True) if title else None
    # Seletores dos dois templates observados, além do template WordPress padrão.
    content = soup.select_one(".site-main-post, .entry-content, [itemprop=articleBody]")
    if content is None:
        for node in soup.select("main.site-main .wrapper-breadcrumbs"):
            if node.select_one("p") and not node.select_one(".title-post"):
                content = node
                break
    text = None
    if content is not None:
        for node in list(content.select(
            "script, style, nav, footer, aside, form, .title-post, .breadcrumbs, "
            ".share-area, .slider-posts, .category-post, .tags, .related-posts, "
            ".related, .post-navigation, .image-post, .entry-meta, .comments-area, "
            ".category-tags, .news-more, .button-back, .sidebar-posts-recentes"
        )):
            node.decompose()
        for node in content.find_all(["p", "div", "li", "h2", "h3", "h4", "blockquote", "br"]):
            node.insert_before("\n")
            node.insert_after("\n")
        text = "\n".join(line for line in (
            " ".join(line.split()) for line in content.get_text(" ", strip=False).splitlines()
        ) if line) or None
    publication_raw = metadata_date(soup, schema, "publication")
    updated_raw = metadata_date(soup, schema, "updated")
    return {"url": url, "titulo": title, "texto": text,
            "publicado_em": parse_date(publication_raw), "atualizado_em": parse_date(updated_raw),
            "coletado_em": datetime.now(timezone.utc).isoformat()}, is_news, publication_raw, updated_raw


def filter_article(article, is_news, artist, year):
    if not is_news:
        return "descartado", "página sem identificação de notícia/post editorial"
    if not article["titulo"] or not article["texto"]:
        return "pendente", "título ou corpo da notícia não extraído; revisar layout"
    if not matches_artist(artist, article["titulo"], article["texto"]):
        return "descartado", "artista ausente do título e do texto principal"
    publication = article["publicado_em"]
    if publication is None:
        return "pendente", "publicação ausente ou não interpretável; ano não confirmado"
    if int(publication[:4]) != year:
        return "descartado", f"ano de publicação {publication[:4]} diferente de {year}"
    return "coletado", "artista presente e ano de publicação confirmado"


def collect(args):
    records, reasons, articles = [], [], []
    started = datetime.now(timezone.utc).isoformat()
    client = HttpClient(args.intervalo, args.timeout, args.tentativas, args.user_agent)
    try:
        urls, search_pages = discover(client, args.artista, args.max_paginas_busca, records, reasons)
        processed = 0
        seen_final_urls = set()
        for url in urls:
            if args.max_paginas is not None and processed >= args.max_paginas:
                records.append({"url": url, "status": "pendente", "etapa": "coleta",
                                "motivo": "não processada: limite de desenvolvimento atingido"})
                reasons.append("limite de páginas candidatas atingido")
                continue
            processed += 1
            LOG.info("Página %d/%d: %s", processed, len(urls), url)
            try:
                html, final_url = client.get_html(url)
                if final_url in seen_final_urls:
                    records.append({"url": url, "status": "descartado", "etapa": "coleta",
                                    "motivo": "URL final duplicada", "url_final": final_url})
                    continue
                seen_final_urls.add(final_url)
                article, is_news, publication_raw, updated_raw = extract(html, final_url)
                status, reason = filter_article(article, is_news, args.artista, args.ano)
                article.update(artista_consultado=args.artista, ano_consultado=args.ano)
                if status == "coletado":
                    articles.append(article)
                else:
                    record = {"url": url, "status": status, "etapa": "coleta", "motivo": reason}
                    if status == "pendente":
                        record.update(dados_extraidos=article, publicacao_original=publication_raw)
                    records.append(record)
                if updated_raw and article["atualizado_em"] is None:
                    records.append({"url": final_url, "status": "pendente", "etapa": "metadados",
                                    "motivo": "atualização não interpretável", "atualizacao_original": updated_raw})
                LOG.info("%s: %s", status, reason)
            except (AccessError, ValueError, TypeError, AttributeError) as exc:
                records.append({"url": url, "status": "falha", "etapa": "coleta", "motivo": str(exc)})
                reasons.append("falha no processamento de páginas")
                LOG.error("Falha por página: %s", exc)
    finally:
        client.close()
    if any(r["status"] == "pendente" for r in records):
        reasons.append("páginas ou metadados pendentes de revisão")
    summary = {"paginas_descobertas": len(urls), "paginas_busca_acessadas": search_pages,
               "paginas_processadas": processed, "noticias_coletadas": len(articles),
               "descartes": sum(r["status"] == "descartado" for r in records),
               "pendencias": sum(r["status"] == "pendente" for r in records),
               "falhas": sum(r["status"] == "falha" for r in records),
               "parcial": bool(reasons), "motivos_resultado_parcial": sorted(set(reasons))}
    return {"consulta": {"artista": args.artista, "ano_publicacao": args.ano},
            "iniciado_em": started, "finalizado_em": datetime.now(timezone.utc).isoformat(),
            "mecanismo_descoberta": "busca interna HTML e links de paginação publicados pelo site",
            "resumo": summary, "urls_descobertas": urls, "noticias": articles, "registros": records}


def save_result(result, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Coleta notícias da Prefeitura por artista e ano de publicação.")
    parser.add_argument("--artista", required=True)
    parser.add_argument("--ano", type=int, required=True)
    parser.add_argument("--max-paginas", type=int, help="limite de notícias candidatas processadas")
    parser.add_argument("--max-paginas-busca", type=int, default=100)
    parser.add_argument("--intervalo", type=float, default=1.0, help="segundos entre acessos (mínimo 1)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--tentativas", type=int, default=3)
    parser.add_argument("--user-agent", default=USER_AGENT, help="identificação do coletor; pode incluir contato")
    parser.add_argument("--saida", type=Path, help="caminho do JSON; padrão: resultados/artista_ano_timestamp.json")
    args = parser.parse_args(argv)
    args.artista = args.artista.strip()
    if not args.artista or not normalize(args.artista) or not 1 <= args.ano <= 9999:
        parser.error("informe artista não vazio e ano entre 1 e 9999")
    if ((args.max_paginas is not None and args.max_paginas < 1) or args.max_paginas_busca < 1
            or not 1 <= args.tentativas <= 5 or not math.isfinite(args.intervalo)
            or not 1 <= args.intervalo <= 60 or not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("limites/timeout devem ser positivos; intervalo de 1 a 60s; tentativas de 1 a 5")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = collect(args)
    slug = re.sub(r"[^a-z0-9]+", "-", normalize(args.artista)).strip("-") or "artista"
    path = args.saida or Path("resultados") / f"{slug}_{args.ano}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json"
    save_result(result, path)
    s = result["resumo"]
    print(f"Descobertas: {s['paginas_descobertas']} | Coletadas: {s['noticias_coletadas']} | "
          f"Descartes: {s['descartes']} | Pendências: {s['pendencias']} | Falhas: {s['falhas']}")
    print(f"Resultado {'PARCIAL' if s['parcial'] else 'da busca acessível'} salvo em: {path.resolve()}")
    if s["parcial"]:
        print("Motivos: " + "; ".join(s["motivos_resultado_parcial"]))
    return 2 if s["falhas"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
