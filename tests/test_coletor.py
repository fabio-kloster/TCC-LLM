import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("coletor", ROOT / "web-scraping.py")
coletor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coletor)
FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://prefeitura.rio/cidade/noticia/"


class ExtractionTests(unittest.TestCase):
    def fixture(self, name):
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_real_root_template_and_timezone(self):
        article, news, _, _ = coletor.extract(self.fixture("noticia_raiz.html"), URL)
        self.assertTrue(news)
        self.assertEqual(article["publicado_em"], "2026-05-03T10:37:17+00:00")
        self.assertEqual(article["atualizado_em"], "2026-05-03T10:50:05+00:00")
        self.assertIn("400 atendimentos", article["texto"])
        self.assertIn("Ações de promoção da saúde", article["texto"])
        for noise in ("Compartilhe", "Marcações", "Outro evento", "Rodapé", "Legenda", "Voltar", "2027"):
            self.assertNotIn(noise, article["texto"])

    def test_real_subdomain_divs_and_visible_publication(self):
        article, news, _, _ = coletor.extract(self.fixture("noticia_subdominio.html"), URL)
        self.assertTrue(news)
        self.assertEqual(article["publicado_em"], "2026-05-05T17:29")
        self.assertIsNone(article["atualizado_em"])
        self.assertIn("362 toneladas", article["texto"])
        self.assertNotIn("Compartilhe", article["texto"])
        self.assertNotIn("Notícias relacionadas", article["texto"])

    def test_only_menu_and_related_mentions_do_not_qualify(self):
        html = '''<body class="single-post"><nav>Shakira</nav><main>
        <h1>Operação de limpeza</h1><div class="entry-content"><p>Equipes em ação.</p>
        <div class="news-more"><h4>Show da Shakira</h4><div class="related-posts">Shakira</div></div>
        </div></main></body>'''
        article, news, _, _ = coletor.extract(html, URL)
        self.assertEqual(coletor.filter_article(article, news, "Shakira", 2026)[0], "descartado")

    def test_missing_publication_is_not_replaced_by_update_or_url(self):
        html = '''<body class="single-post"><meta property="article:modified_time" content="2026-04-01">
        <main><h1>Shakira</h1><div class="entry-content"><p>Show em 2026.</p></div></main></body>'''
        article, news, _, _ = coletor.extract(html, URL + "2026/04/01/")
        self.assertIsNone(article["publicado_em"])
        self.assertEqual(coletor.filter_article(article, news, "Shakira", 2026)[0], "pendente")

    def test_bad_publication_is_pending(self):
        html = self.fixture("noticia_raiz.html").replace("2026-05-03T10:37:17+00:00", "data inválida")
        article, news, raw, _ = coletor.extract(html, URL)
        self.assertEqual(raw, "data inválida")
        self.assertIsNone(article["publicado_em"])
        self.assertEqual(coletor.filter_article(article, news, "Shakira", 2026)[0], "pendente")

    def test_schema_article_publication(self):
        html = '''<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle",
        "datePublished":"2025-12-31", "dateModified":"2026-01-01"}]}</script>
        <main><h1>Shakira em 2026</h1><div class="entry-content"><p>Show em 2026.</p></div></main>'''
        article, news, _, _ = coletor.extract(html, URL)
        self.assertTrue(news)
        self.assertEqual(article["publicado_em"], "2025-12-31")
        self.assertEqual(coletor.filter_article(article, news, "Shakira", 2026)[0], "descartado")


class RulesTests(unittest.TestCase):
    def test_artist_case_accents_spacing_and_word_boundaries(self):
        self.assertTrue(coletor.matches_artist("Beyoncé", "BEYONCE no Rio", ""))
        self.assertTrue(coletor.matches_artist("Lady Gaga", "", "Show de Lady\nGaga."))
        self.assertFalse(coletor.matches_artist("Ana", "Copacabana", "Banana"))
        self.assertFalse(coletor.matches_artist("Shakira", "Show", "Outro artista"))

    def test_publication_year_not_event_or_update_year(self):
        article = {"titulo": "Shakira", "texto": "Evento em 2026",
                   "publicado_em": "2025-12-31T23:30:00-03:00", "atualizado_em": "2026-01-01"}
        self.assertEqual(coletor.filter_article(article, True, "Shakira", 2025)[0], "coletado")
        self.assertEqual(coletor.filter_article(article, True, "Shakira", 2026)[0], "descartado")

    def test_dates_do_not_invent_time_or_timezone(self):
        for raw, expected in [("1 de março de 2026", "2026-03-01"), ("02/05/2026", "2026-05-02"),
                              ("02/05/2026 - 17:29", "2026-05-02T17:29"),
                              ("2026-05-02T17:29-03:00", "2026-05-02T17:29-03:00"),
                              ("2026-05-02T18:30:00-03:00", "2026-05-02T18:30:00-03:00"),
                              ("2026-05-02T21:30:00Z", "2026-05-02T21:30:00+00:00"),
                              ("31/02/2026", None), ("2026", None), (None, None)]:
            with self.subTest(raw=raw):
                self.assertEqual(coletor.parse_date(raw), expected)

    def test_domain_restriction_and_tracking_deduplication(self):
        self.assertTrue(coletor.official_url("https://saude.prefeitura.rio/noticias/"))
        for url in ["https://prefeitura.rio.evil.test/", "https://evilprefeitura.rio/", "file:///x"]:
            self.assertFalse(coletor.official_url(url))
        self.assertEqual(coletor.normalize_url(URL + "?utm_source=search#x"), URL)


class DiscoveryTests(unittest.TestCase):
    SEARCH = '''<article class="section-content-categories"><a href="/cidade/noticia/"><h3>Notícia</h3></a>
    </article><article class="section-content-categories"><a href="/cidade/noticia/?utm_source=x"><h3>Duplicada</h3></a>
    </article><a href="https://reflorestario.prefeitura.rio/page/2/?s=Shakira">2</a>'''

    def test_automatic_discovery_dedup_and_pagination_failure(self):
        client = Mock()
        client.get_html.side_effect = [(self.SEARCH, coletor.BASE + "?s=Shakira"), coletor.AccessError("HTTP 404")]
        records, reasons = [], []
        urls, pages = coletor.discover(client, "Shakira", 100, records, reasons)
        self.assertEqual(urls, [URL])
        self.assertEqual(pages, 2)
        self.assertEqual(records[0]["status"], "falha")
        self.assertTrue(reasons)

    def test_repeated_pagination_stops_and_is_partial(self):
        client = Mock()
        repeated = self.SEARCH.replace('href="/cidade/', 'href="https://prefeitura.rio/cidade/')
        client.get_html.side_effect = [(repeated, coletor.BASE + "?s=Shakira"),
                                       (repeated, "https://reflorestario.prefeitura.rio/page/2/?s=Shakira")]
        records, reasons = [], []
        urls, _ = coletor.discover(client, "Shakira", 100, records, reasons)
        self.assertEqual(urls, [URL])
        self.assertIn("paginação repetida", reasons)

    def test_unrecognized_search_is_not_silent_zero_results(self):
        client = Mock()
        client.get_html.return_value = ("<h1>Site em manutenção</h1>", coletor.BASE)
        records, reasons = [], []
        coletor.discover(client, "Shakira", 100, records, reasons)
        self.assertEqual(records[0]["status"], "pendente")
        self.assertTrue(reasons)

    def test_page_limit_records_every_unprocessed_url(self):
        args = SimpleNamespace(artista="Shakira", ano=2026, intervalo=1, timeout=30,
                               tentativas=3, user_agent=coletor.USER_AGENT,
                               max_paginas_busca=10, max_paginas=1)
        html = (FIXTURES / "noticia_raiz.html").read_text(encoding="utf-8")
        client = Mock()
        client.get_html.return_value = (html, URL)
        with patch.object(coletor, "HttpClient", return_value=client), patch.object(
            coletor, "discover", return_value=([URL, URL + "outro/"], 1)
        ):
            result = coletor.collect(args)
        self.assertEqual(result["resumo"]["noticias_coletadas"], 1)
        self.assertEqual(result["resumo"]["paginas_processadas"], 1)
        self.assertTrue(result["resumo"]["parcial"])
        self.assertEqual(result["registros"][0]["url"], URL + "outro/")
        client.close.assert_called_once()

    def test_page_failure_does_not_stop_next_page(self):
        args = SimpleNamespace(artista="Shakira", ano=2026, intervalo=1, timeout=30,
                               tentativas=3, user_agent=coletor.USER_AGENT,
                               max_paginas_busca=10, max_paginas=None)
        html = (FIXTURES / "noticia_raiz.html").read_text(encoding="utf-8")
        client = Mock()
        client.get_html.side_effect = [coletor.AccessError("HTTP 503"), (html, URL)]
        with patch.object(coletor, "HttpClient", return_value=client), patch.object(
            coletor, "discover", return_value=([URL + "indisponivel/", URL], 1)
        ):
            result = coletor.collect(args)
        self.assertEqual(result["resumo"]["paginas_processadas"], 2)
        self.assertEqual(result["resumo"]["noticias_coletadas"], 1)
        self.assertEqual(result["resumo"]["falhas"], 1)
        self.assertTrue(result["resumo"]["parcial"])


class HttpTests(unittest.TestCase):
    def test_robots_blocks_request_and_external_redirect_never_fetched(self):
        client = coletor.HttpClient()
        robots = Mock(status_code=200, text="User-agent: *\nDisallow: /proibido/", headers={})
        redirect = Mock(status_code=302, headers={"Location": "https://externo.test/"})
        with patch.object(client, "_request", side_effect=[robots, redirect]) as request:
            with self.assertRaises(coletor.AccessError):
                client.get_html("https://prefeitura.rio/proibido/")
            self.assertEqual(request.call_count, 1)
            with self.assertRaises(coletor.AccessError):
                client.get_html(URL)
            self.assertEqual(request.call_count, 2)
        client.close()

    def test_temporary_server_failure_retries_and_respects_retry_after(self):
        client = coletor.HttpClient(attempts=2)
        limited = Mock(status_code=429, headers={"Retry-After": "5"})
        success = Mock(status_code=200)
        with patch.object(client.session, "get", side_effect=[limited, success]) as get, patch.object(
            coletor.time, "sleep"
        ) as sleep:
            self.assertIs(client._request(URL, 1), success)
            self.assertEqual(get.call_count, 2)
            sleep.assert_any_call(5.0)
        client.close()

    def test_unavailable_robots_prevents_page_access(self):
        client = coletor.HttpClient()
        with patch.object(client, "_request", side_effect=coletor.AccessError("HTTP 503")) as request:
            with self.assertRaises(coletor.AccessError):
                client.get_html(URL)
            self.assertEqual(request.call_count, 1)
        client.close()


if __name__ == "__main__":
    unittest.main()
