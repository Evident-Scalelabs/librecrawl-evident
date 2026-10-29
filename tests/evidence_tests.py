"""Offline HTTP evidence, persistence and private-service fixtures.

Run with: python tests/evidence_tests.py
No browser, external HTTP server or model/provider call is used.
"""
import base64
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from urllib3.response import HTTPResponse
from src import auth_db, crawl_db
from src.crawler import HTTP_USER_AGENT, RobotsAccessDenied, WebCrawler
from src.core.rate_limiter import RateLimiter


class EvidenceFixtures(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        data = Path(self.directory.name) / 'data'
        data.mkdir()
        database = str(data / 'users.db')
        self.patches = [patch.object(auth_db, 'DB_FILE', database),
                        patch.object(crawl_db, 'DB_FILE', database),
                        patch.dict(os.environ, {'LOCAL_MODE': 'true', 'SECRET_KEY': 'fixture-session-signing-key'})]
        for item in self.patches:
            item.start()
        auth_db.init_db()
        with patch.object(sys, 'argv', [sys.argv[0]]):
            self.web = importlib.import_module('main')
        # Register the service hooks before any fixture makes a Flask request.
        with patch.dict(os.environ, {'LIBRECRAWL_API_KEY': 'fixture-service-key'}):
            importlib.import_module('service')
        source_path = patch.object(self.web, '__file__', str(Path(self.directory.name) / 'main.py'))
        source_path.start()
        self.patches.append(source_path)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def make_crawl(self, private_service=False):
        with patch.dict(os.environ, {'LIBRECRAWL_PRIVATE_SERVICE': str(private_service).lower()}):
            crawler = WebCrawler()
        crawler.base_url = 'https://example.test'
        crawler.base_domain = 'example.test'
        crawler.config.update({'delay': 0, 'max_file_size': 0,
                               'discover_sitemaps': False, 'respect_robots': False})
        crawler._initialize_components()
        crawler.crawl_id = crawl_db.create_crawl(None, 'fixture', crawler.base_url,
                                               crawler.base_domain, crawler.config)
        crawler.db_save_enabled = True
        return crawler

    def test_response_bytes_survive_sqlite_and_json_exports(self):
        body = '<html><head><title>Café</title></head><body><p>Exact terms €5.</p></body></html>'.encode()
        response = requests.Response()
        response.status_code = 200
        response._content = body
        response.encoding = 'utf-8'
        response.url = 'https://example.test/final'
        response.headers.update({'Content-Type': 'text/html; charset=utf-8', 'ETag': 'fixture-v1'})
        redirect = requests.Response()
        redirect.status_code = 302
        redirect.url = 'https://example.test/start'
        response.history = [redirect]
        crawler = self.make_crawl()
        with patch.object(crawler.session, 'get', return_value=response):
            captured = crawler._crawl_url_with_requests(redirect.url, 0)
        self.assertTrue(crawl_db.save_url_batch(crawler.crawl_id, [captured]))
        restored, = crawl_db.load_crawled_urls(crawler.crawl_id)
        fields = ['url', 'final_url', 'body_base64', 'body_text', 'body_sha256',
                  'response_headers', 'fetched_at', 'capture_method']
        exports = [json.loads(self.web.generate_json_export([restored], fields)),
                   json.loads(''.join(self.web._stream_urls_json([restored], fields)))]
        for exported in exports:
            record, = exported['data']
            self.assertEqual(base64.b64decode(record['body_base64']), body)
            self.assertEqual(record['body_sha256'], hashlib.sha256(body).hexdigest())
            self.assertIn('Exact terms €5.', record['body_text'])
            self.assertEqual(record['url'], redirect.url)
            self.assertEqual(record['final_url'], response.url)
            self.assertEqual(record['response_headers']['ETag'], 'fixture-v1')
            self.assertTrue(record['fetched_at'].endswith('+00:00'))
            self.assertEqual(record['capture_method'], 'http')

    def test_get_cap_applies_to_decoded_body_without_content_length(self):
        crawler = self.make_crawl()
        crawler.config['max_file_size'] = 64
        body = b'<html><title>Unusable</title><body>' + b'x' * 4096 + b'</body></html>'
        responses = []

        def respond(adapter, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            response.headers.update({'Content-Type': 'text/html', 'Content-Encoding': 'gzip'})
            response.raw = HTTPResponse(body=io.BytesIO(gzip.compress(body)),
                                        headers=dict(response.headers), preload_content=False)
            responses.append(response)
            return response

        # Neither HEAD nor GET supplies a Content-Length. Exercise the actual
        # requests response-hook/decoding path, with no external HTTP request.
        with patch.object(requests.adapters.HTTPAdapter, 'send', new=respond), \
                patch.object(crawler.seo_extractor, 'extract_basic_seo_data') as parse:
            captured = crawler._crawl_url_with_requests('https://example.test/large', 0)
        self.assertEqual(len(responses), 1)  # An oversize body is not retried.
        self.assertTrue(responses[0].raw.closed)
        self.assertEqual(captured['status_code'], 200)
        self.assertEqual(captured['error_type'], 'file_too_large')
        self.assertEqual(captured['size'], 65)
        self.assertIn('not retained or analyzed', captured['error'])
        parse.assert_not_called()
        crawler.issue_detector.detect_issues(captured)
        self.assertEqual(crawler.issue_detector.get_issues(), [])
        self.assertTrue(crawl_db.save_url_batch(crawler.crawl_id, [captured]))
        restored, = crawl_db.load_crawled_urls(crawler.crawl_id)
        self.assertEqual(restored['error_type'], 'file_too_large')
        self.assertEqual(restored['final_url'], captured['url'])
        for field in ('body_base64', 'body_text', 'body_sha256'):
            self.assertIsNone(restored[field])

    def test_stream_read_failure_closes_response_and_uses_existing_retry(self):
        crawler = self.make_crawl()
        crawler.config.update({'max_file_size': 64, 'retries': 1})
        responses = []

        class BrokenBody(io.BytesIO):
            def read(self, *args, **kwargs):
                raise OSError('fixture interrupted body')

        def respond(adapter, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            response.headers['Content-Type'] = 'text/plain'
            source = BrokenBody() if not responses else io.BytesIO(b'complete retry')
            response.raw = HTTPResponse(body=source, preload_content=False)
            responses.append(response)
            return response

        with patch.object(requests.adapters.HTTPAdapter, 'send', new=respond), \
                patch('src.crawler.time.sleep'):
            captured = crawler._crawl_url_with_requests('https://example.test/retry', 0)
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(response.raw.closed for response in responses))
        self.assertIsNone(captured['error_type'])
        self.assertEqual(base64.b64decode(captured['body_base64']), b'complete retry')

    def test_failed_batch_keeps_pending_rows_and_retry_does_not_duplicate(self):
        crawler = self.make_crawl()
        first = {'url': 'https://example.test/first', 'status_code': 200}
        later = {'url': 'https://example.test/later', 'status_code': 200}
        crawler.unsaved_urls.append(first)
        crawler.unsaved_links.append({'source_url': first['url'], 'target_url': later['url']})
        save = crawl_db.save_url_batch

        def fail_once(crawl_id, rows):
            crawler.unsaved_urls.append(later)
            with patch.object(crawl_db, 'get_db', side_effect=sqlite3.OperationalError('fixture database unavailable')):
                return save(crawl_id, rows)

        with patch.object(crawl_db, 'save_url_batch', side_effect=fail_once):
            self.assertFalse(crawler._save_batch_to_db(force=True))
        self.assertEqual(crawler.unsaved_urls, [first, later])
        self.assertEqual(crawler.get_status_light()['status'], 'failed')
        self.assertTrue(crawler.get_status_light()['persistence_error'])
        self.assertTrue(crawler._save_batch_to_db(force=True))
        self.assertTrue(crawler._save_batch_to_db(force=True))
        self.assertEqual(crawler.unsaved_urls, [])
        self.assertEqual(len(crawl_db.load_crawled_urls(crawler.crawl_id)), 2)
        self.assertEqual(len(crawl_db.load_crawl_links(crawler.crawl_id)), 1)
        self.assertIs(save, crawl_db.save_url_batch)

    def test_ancillary_http_and_gzip_file_limits_record_incomplete_evidence(self):
        crawler = self.make_crawl()
        crawler.update_config({'max_file_size': 128})
        inflated = b'<urlset><url><loc>https://example.test/' + b'x' * 4096 + b'</loc></url></urlset>'
        responses = []

        def respond(adapter, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            response.headers['Content-Type'] = 'application/xml'
            if not request.url.endswith('.gz'):
                response.headers['Content-Encoding'] = 'gzip'
            response.raw = HTTPResponse(body=io.BytesIO(gzip.compress(inflated)),
                                        headers=dict(response.headers), preload_content=False)
            responses.append(response)
            return response

        with patch.object(requests.adapters.HTTPAdapter, 'send', new=respond), \
                patch('src.core.sitemap_parser.ET.fromstring') as parse:
            # Existing robots permissive policy remains an explicit unknown gap.
            self.assertTrue(crawler._check_robots_txt('https://other.test/page'))
            self.assertEqual(crawler.sitemap_parser._get_sitemaps_from_robots(crawler.base_url), [])
            self.assertEqual(crawler.sitemap_parser._parse_sitemap(crawler.base_url + '/sitemap.xml'), [])
            self.assertEqual(crawler.sitemap_parser._parse_sitemap(crawler.base_url + '/sitemap.xml.gz'), [])
            parse.assert_not_called()
        self.assertEqual(len(responses), 4)
        self.assertTrue(all(response.raw.closed for response in responses))
        self.assertTrue(crawler._save_batch_to_db(force=True))
        issues = crawl_db.load_crawl_issues(crawler.crawl_id)
        self.assertEqual(len(issues), 4)
        self.assertTrue(all(issue['category'] == 'Acquisition' for issue in issues))
        self.assertTrue(all('exceeds 128 bytes' in issue['details'] for issue in issues))

    @staticmethod
    def retained_fixture_row(crawler, url, depth=0):
        body = ('retained ' + url).encode()
        row = crawler.seo_extractor.create_empty_result(url, depth, 200)
        row.update(content_type='text/plain', body_base64=base64.b64encode(body).decode(),
                   body_sha256=hashlib.sha256(body).hexdigest(), final_url=url,
                   capture_method='http', size=len(body))
        return row

    def test_active_page_reservations_do_not_dispatch_past_cap(self):
        crawler = self.make_crawl()
        crawler.config.update({'max_urls': 2, 'concurrency': 3, 'enable_duplication_check': False})
        root = self.retained_fixture_row(crawler, crawler.base_url)
        crawler.crawl_results.append(root)
        crawler.unsaved_urls.append(root)
        crawler.pages_crawled = crawler.stats['crawled'] = 1
        for n in range(3):
            crawler.link_manager.add_url(crawler.base_url + f'/child-{n}', 1)
        dispatched = []

        def fetch(url, depth):
            dispatched.append(url)
            time.sleep(0.01)
            return self.retained_fixture_row(crawler, url, depth)

        crawler.is_running = True
        with patch.object(crawler, '_crawl_url', side_effect=fetch):
            crawler._crawl_worker()
        self.assertEqual(len(dispatched), 1)
        restored = crawl_db.load_crawled_urls(crawler.crawl_id)
        self.assertEqual(len(restored), 2)
        self.assertTrue(all(row['body_base64'] for row in restored))

    def test_stop_drains_reserved_results_and_refuses_instance_reuse(self):
        crawler = self.make_crawl()
        crawler.config.update({'max_urls': 10, 'concurrency': 2, 'enable_duplication_check': False})
        for n in range(3):
            crawler.link_manager.add_url(crawler.base_url + f'/child-{n}', 1)
        started = threading.Barrier(3)
        release = threading.Event()
        dispatched = []

        def fetch(url, depth):
            dispatched.append(url)
            started.wait(timeout=3)
            if not release.wait(timeout=3):
                raise RuntimeError('fixture release timed out')
            return self.retained_fixture_row(crawler, url, depth)

        crawler.is_running = True
        with patch.object(crawler, '_crawl_url', side_effect=fetch):
            crawler.crawl_thread = threading.Thread(target=crawler._crawl_worker)
            crawler.crawl_thread.start()
            try:
                started.wait(timeout=3)
                self.assertFalse(crawler.pause_crawl()[0])
                self.assertEqual(crawler.get_status_light()['status'], 'pausing')
                self.assertFalse(crawler.stop_crawl(wait_timeout=0)[0])
                self.assertEqual(crawler.get_status_light()['status'], 'stopping')
                self.assertFalse(crawler.start_crawl(crawler.base_url)[0])
                with self.assertRaises(RuntimeError):
                    crawler.load_data({}, [], [], [])
            finally:
                release.set()
                crawler.crawl_thread.join(timeout=3)
        self.assertFalse(crawler.crawl_thread.is_alive())
        self.assertTrue(crawler.stop_crawl(wait_timeout=0)[0])
        self.assertEqual(len(dispatched), 2)
        restored = crawl_db.load_crawled_urls(crawler.crawl_id)
        self.assertEqual(len(restored), 2)
        self.assertTrue(all(row['body_base64'] for row in restored))

    def test_cleanup_and_shutdown_keep_inactive_failed_batches(self):
        crawler = self.make_crawl()
        crawler.unsaved_urls.append(self.retained_fixture_row(crawler, crawler.base_url))
        instances = {'fixture': {'crawler': crawler, 'settings': None,
                                 'last_accessed': datetime.now() - timedelta(hours=2)}}
        with patch.object(self.web, 'crawler_instances', instances), \
                patch.object(self.web, 'SHUTTING_DOWN', False):
            with patch.object(crawl_db, 'save_url_batch', return_value=False):
                self.web.cleanup_old_instances()
                self.assertIn('fixture', instances)
                self.assertEqual(len(crawler.unsaved_urls), 1)
                with self.assertRaises(SystemExit) as failure:
                    self.web.graceful_shutdown(None, None)
                self.assertEqual(failure.exception.code, 1)
                self.assertEqual(len(crawler.unsaved_urls), 1)
            self.assertEqual(self.web.app.test_client().post('/api/start_crawl').status_code, 503)
            with self.assertRaises(SystemExit) as success:
                self.web.graceful_shutdown(None, None)
            self.assertEqual(success.exception.code, 0)
            self.assertEqual(len(crawl_db.load_crawled_urls(crawler.crawl_id)), 1)
            self.web.cleanup_old_instances()
            self.assertNotIn('fixture', instances)

    def test_shared_http_session_paces_get_head_and_redirects(self):
        crawler = WebCrawler()
        crawler.config['delay'] = 0.04
        crawler.rate_limiter = RateLimiter(25)
        calls = []

        def respond(adapter, request, **kwargs):
            calls.append((request.method, request.url, time.monotonic()))
            response = requests.Response()
            response.request = request
            response.url = request.url
            response._content = b''
            response._content_consumed = True
            response.status_code = 302 if request.url.endswith('/redirect') else 200
            if response.status_code == 302:
                response.headers['Location'] = '/final'
            return response

        with patch.object(requests.adapters.HTTPAdapter, 'send', new=respond):
            crawler.session.get('https://example.test/redirect')
            crawler.session.head('https://example.test/image.png')
        self.assertEqual([entry[0] for entry in calls], ['GET', 'GET', 'HEAD'])
        self.assertTrue(all(b[2] - a[2] >= 0.035 for a, b in zip(calls, calls[1:])), calls)

    def test_stopped_crawl_does_not_dispatch_queued_http(self):
        crawler = WebCrawler()
        crawler.config['delay'] = 1
        crawler.rate_limiter = RateLimiter(1)
        with patch.object(requests.adapters.HTTPAdapter, 'send') as send, \
                patch.object(crawler.rate_limiter, 'acquire') as acquire:
            crawler._stop_event.set()
            for method in ('GET', 'HEAD'):
                with self.subTest(method=method), self.assertRaisesRegex(requests.RequestException, 'Crawl stopped'):
                    crawler.session.request(method, 'https://example.test/queued')
            acquire.assert_not_called()
            crawler._stop_event.clear()
            acquire.side_effect = crawler._stop_event.set
            with self.assertRaisesRegex(requests.RequestException, 'Crawl stopped'):
                crawler.session.get('https://example.test/waiting')
            send.assert_not_called()

    def test_private_http_policy_covers_head_redirects_sitemaps_and_unknown_robots(self):
        crawler = self.make_crawl(private_service=True)
        crawler.config['respect_robots'] = True
        calls = []

        def respond(adapter, request, **kwargs):
            calls.append((request.method, request.url, request.headers['User-Agent'], kwargs.get('proxies')))
            response = requests.Response()
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            response.url = request.url
            response.request = request
            response.status_code = 200
            body = b'<html><title>Allowed</title><a href="/private/discovered">Private</a></html>'
            if request.url.endswith('/robots.txt'):
                if 'policy-redirect.test' in request.url:
                    response.status_code = 302
                    response.headers['Location'] = '/policy'
                    body = b''
                elif 'unavailable.test' in request.url:
                    response.status_code = 503
                    body = b'unavailable'
                elif 'missing.test' in request.url:
                    response.status_code = 404
                    body = b''
                elif 'forbidden.test' in request.url:
                    response.status_code = 403
                    body = b'Forbidden'
                elif 'unauthorized.test' in request.url:
                    response.status_code = 401
                    body = b'Unauthorized'
                elif 'timeout.test' in request.url:
                    raise requests.Timeout('fixture timeout')
                elif 'oversized.test' in request.url:
                    body = b'x' * 257
                elif 'stream-failure.test' in request.url:
                    def broken_body(**kwargs):
                        raise requests.exceptions.ChunkedEncodingError('fixture truncated body')
                        yield b''
                    response.iter_content = broken_body
                else:
                    body = b'User-agent: *\nDisallow: /private\nDisallow: /sitemap.xml\n'
            elif request.url == 'https://policy-redirect.test/policy':
                body = b'User-agent: *\nDisallow: /private\n'
            elif request.url.endswith('/redirect'):
                response.status_code = 302
                response.headers['Location'] = '/private'
                body = b''
            response.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False)
            return response

        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://ambient.invalid:8080'}), \
                patch.object(requests.adapters.HTTPAdapter, 'send', new=respond):
            self.assertFalse(crawler.session.trust_env)
            for method in ('GET', 'HEAD'):
                with self.subTest(method=method), self.assertRaises(RobotsAccessDenied):
                    crawler.session.request(method, crawler.base_url + '/private')
            redirected = crawler._crawl_url_with_requests(crawler.base_url + '/redirect', 0)
            self.assertEqual(redirected['error_type'], 'robots_disallowed')
            self.assertEqual(crawler.sitemap_parser._parse_sitemap(crawler.base_url + '/sitemap.xml'), [])
            unavailable = crawler._crawl_url_with_requests('https://unavailable.test/page', 0)
            self.assertEqual(unavailable['error_type'], 'robots_unavailable')
            forbidden = crawler._crawl_url_with_requests('https://forbidden.test/page', 0)
            self.assertEqual(forbidden['error_type'], 'robots_disallowed')
            unauthorized = crawler._crawl_url_with_requests('https://unauthorized.test/page', 0)
            timeout = crawler._crawl_url_with_requests('https://timeout.test/page', 0)
            crawler.config['max_file_size'] = 256
            oversized = crawler._crawl_url_with_requests('https://oversized.test/page', 0)
            stream_failure = crawler._crawl_url_with_requests('https://stream-failure.test/page', 0)
            crawler.config['max_file_size'] = 0
            self.assertEqual(crawler.session.get('https://missing.test/page').status_code, 200)
            allowed = crawler._crawl_url_with_requests(crawler.base_url + '/public', 0)
            self.assertEqual(allowed['status_code'], 200)
            self.assertEqual(crawler.session.get('https://policy-redirect.test/page').status_code, 200)
            policy_redirect = crawler._crawl_url_with_requests('https://policy-redirect.test/private', 0)
            denied = [redirected, unavailable, forbidden, unauthorized, timeout,
                      oversized, stream_failure, policy_redirect]
            # Cached refusals, including discovery, retain the same provenance and do not refetch.
            for row in denied:
                with self.assertRaises(RobotsAccessDenied):
                    crawler.session.head(row['url'], allow_redirects=True)
            with self.assertRaises(RobotsAccessDenied):
                crawler.session.head(crawler.base_url + '/private/discovered')
        dispatched = [url for _, url, _, _ in calls]
        self.assertNotIn(crawler.base_url + '/private', dispatched)
        self.assertNotIn(crawler.base_url + '/sitemap.xml', dispatched)
        self.assertNotIn('https://unavailable.test/page', dispatched)
        self.assertNotIn('https://forbidden.test/page', dispatched)
        self.assertNotIn(crawler.base_url + '/private/discovered', dispatched)
        self.assertNotIn('https://policy-redirect.test/private', dispatched)
        self.assertEqual(dispatched.count('https://policy-redirect.test/robots.txt'), 1)
        self.assertIn('https://policy-redirect.test/policy', dispatched)
        self.assertEqual(dispatched.count('https://unavailable.test/robots.txt'), 1)
        self.assertTrue(all(agent == HTTP_USER_AGENT and not proxies for _, _, agent, proxies in calls))
        self.assertTrue(crawler._save_batch_to_db(force=True))
        issues = crawl_db.load_crawl_issues(crawler.crawl_id)
        discovered_gaps = [issue for issue in issues if issue['url'] == crawler.base_url + '/private/discovered'
                           and issue['category'] == 'Acquisition']
        self.assertEqual(len(discovered_gaps), 1)
        self.assertIn('robots_disallowed', discovered_gaps[0]['details'])
        fields = ['robots_status', 'robots_url', 'robots_final_url', 'robots_reason']
        expected = [(200, 'rule_disallow'), (503, 'http_error'), (403, 'http_401_403'),
                    (401, 'http_401_403'), (None, 'fetch_error'), (200, 'body_limit'),
                    (200, 'fetch_error'), (200, 'rule_disallow')]
        for row, (status, reason) in zip(denied, expected):
            with self.subTest(url=row['url']):
                self.assertEqual(row['status_code'], 0)
                self.assertEqual(row['robots_status'], status)
                self.assertEqual(row['robots_reason'], reason)
                self.assertFalse(row.get('body_base64'))
                robots_url = row['robots_url']
                self.assertEqual(dispatched.count(robots_url), 1)
                if row is not redirected:
                    self.assertNotIn(row['url'], dispatched)
        self.assertIsNone(timeout['robots_final_url'])
        self.assertEqual(policy_redirect['robots_final_url'], 'https://policy-redirect.test/policy')
        self.assertEqual(discovered_gaps[0]['robots_status'], 200)
        self.assertEqual(discovered_gaps[0]['robots_reason'], 'rule_disallow')
        self.assertTrue(crawl_db.save_url_batch(crawler.crawl_id, denied))
        restored = crawl_db.load_crawled_urls(crawler.crawl_id)
        for actual, original in zip(restored, denied):
            self.assertEqual({f: actual[f] for f in fields}, {f: original[f] for f in fields})
        for output in (self.web.generate_json_export(restored, fields),
                       ''.join(self.web._stream_urls_json(restored, fields))):
            exported = json.loads(output)['data']
            self.assertEqual(exported, [{f: row[f] for f in fields} for row in denied])
        streamed_issues = json.loads(''.join(self.web._stream_issues_json(issues)))['data']
        grouped_issues = json.loads(self.web.generate_issues_json_export(issues))['issues_by_url']
        for issue in issues:
            if issue.get('robots_url'):
                projected = next(row for row in streamed_issues if row['url'] == issue['url']
                                 and row['details'] == issue['details'])
                grouped = next(row for row in grouped_issues[issue['url']]
                               if row['details'] == issue['details'])
                for projection in (projected, grouped):
                    self.assertEqual({f: projection[f] for f in fields}, {f: issue[f] for f in fields})
        # HTTP status/refusal provenance is retained by both public read routes after reload.
        crawler.load_data(crawl_db.get_crawl_by_id(crawler.crawl_id), restored, [], issues)
        with patch.object(self.web, 'get_or_create_crawler', return_value=crawler), \
                patch.dict(os.environ, {'LIBRECRAWL_API_KEY': 'fixture-service-key'}):
            client = self.web.app.test_client()
            headers = {'Authorization': 'Bearer fixture-service-key'}
            live = client.get('/api/crawl_status', headers=headers).get_json()
            with client.session_transaction() as session:
                actor = session['user_id']
            with crawl_db.get_db() as connection:
                connection.execute('UPDATE crawls SET user_id = ? WHERE id = ?',
                                   (actor, crawler.crawl_id))
            saved = client.get(f'/api/crawls/{crawler.crawl_id}', headers=headers).get_json()
            for payload in (live, saved):
                self.assertEqual(payload['urls'][2]['robots_status'], 403)
                self.assertTrue(any(i.get('robots_reason') == 'rule_disallow' for i in payload['issues']))

    def test_robots_columns_migrate_old_rows_without_inventing_evidence(self):
        crawler = self.make_crawl()
        fields = ['robots_status', 'robots_url', 'robots_final_url', 'robots_reason']
        self.assertTrue(crawl_db.save_url_batch(crawler.crawl_id, [{'url': 'https://example.test/old'}]))
        self.assertTrue(crawl_db.save_issues_batch(crawler.crawl_id, [{'url': 'https://example.test/old'}]))
        with crawl_db.get_db() as connection:
            for table in ('crawled_urls', 'crawl_issues'):
                for field in fields:
                    connection.execute(f'ALTER TABLE {table} DROP COLUMN {field}')
        crawl_db.init_crawl_tables()
        crawl_db.init_crawl_tables()  # Additive migration remains idempotent.
        for row in (crawl_db.load_crawled_urls(crawler.crawl_id)[0],
                    crawl_db.load_crawl_issues(crawler.crawl_id)[0]):
            self.assertTrue(all(row[field] is None for field in fields))

    def test_private_api_disables_ancillary_dispatch_but_retains_parsed_evidence(self):
        calls = []
        page = (b'<html><title>Bounded page</title><a href="https://external.test/offer">Offer</a>'
                b'<img src="https://cdn.test/logo.png" alt="Logo"><img src="/local.png"></html>')

        def respond(adapter, request, **kwargs):
            calls.append((request.method, request.url))
            self.assertEqual(request.method, 'GET')
            self.assertIn(request.url, ('https://example.test/robots.txt', 'https://example.test/page'))
            response = requests.Response()
            response.url = request.url
            response.request = request
            response.status_code = 200
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            body = b'User-agent: *\nAllow: /\n' if request.url.endswith('/robots.txt') else page
            response.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False)
            return response

        with patch.dict(os.environ, {'LIBRECRAWL_PRIVATE_SERVICE': 'true'}), \
                patch.object(requests.adapters.HTTPAdapter, 'send', new=respond), \
                self.web.app.test_client() as client:
            headers = {'Authorization': 'Bearer fixture-service-key'}
            settings = {'maxDepth': 1, 'maxUrls': 1, 'maxFileSize': 1, 'crawlDelay': 0,
                        'concurrency': 1, 'discoverSitemaps': False, 'enableDuplicationCheck': False,
                        'enableLinkChecks': False, 'crawlExternalLinks': False, 'crawlImages': False}
            saved = client.post('/api/save_settings', headers=headers, json=settings)
            self.assertTrue(saved.get_json()['success'])
            self.assertFalse(self.web.get_session_settings().get_crawler_config()['enable_link_checks'])
            self.assertEqual(client.get('/healthz').get_json()['requestPolicy']['linkChecks'], 'disabled')
            started = client.post('/api/start_crawl', headers=headers, json={'url': 'https://example.test/page'})
            self.assertTrue(started.get_json()['success'])
            crawler = self.web.get_or_create_crawler()
            crawler.crawl_thread.join(timeout=3)
            crawler.auto_save_thread.join(timeout=1)
            self.assertFalse(crawler._has_live_threads())
            payload = client.get(f"/api/crawls/{started.get_json()['crawl_id']}", headers=headers).get_json()
            self.assertEqual(calls, [('GET', 'https://example.test/robots.txt'),
                                     ('GET', 'https://example.test/page')])
            self.assertEqual(len(payload['urls']), 1)
            row = payload['urls'][0]
            self.assertEqual(base64.b64decode(row['body_base64']), page)
            self.assertEqual(len(row['images']), 2)
            self.assertEqual({link['target_url'] for link in payload['links']},
                             {'https://external.test/offer', 'https://cdn.test/logo.png',
                              'https://example.test/local.png'})
            self.assertTrue(all(link['target_status'] is None for link in payload['links']))
            # The dispatch owner also refuses an internal opt-in and legacy missing config.
            for config in ({'enable_link_checks': True}, {}):
                crawler.config.pop('enable_link_checks', None)
                crawler.config.update(config)
                crawler._head_check_links(payload['links'])
            self.assertEqual(len(calls), 2)

    def test_upstream_link_checks_remain_enabled_by_default_and_can_be_disabled(self):
        from src.settings_manager import SettingsManager
        with patch.dict(os.environ, {'LIBRECRAWL_PRIVATE_SERVICE': 'false'}):
            settings = SettingsManager(tier='admin')
            self.assertTrue(settings.get_crawler_config()['enable_link_checks'])
            crawler = self.make_crawl()
        self.assertTrue(crawler.config['enable_link_checks'])
        crawler.config.pop('enable_link_checks')  # Historical upstream config remains enabled.
        calls = []

        def respond(adapter, request, **kwargs):
            calls.append((request.method, request.url))
            response = requests.Response()
            response.url = request.url
            response.request = request
            response.status_code = 405 if request.method == 'HEAD' else 200
            response.headers.update({'Content-Type': 'image/png', 'Content-Length': '0'})
            response.raw = HTTPResponse(body=io.BytesIO(b''), preload_content=False)
            return response

        with patch.object(requests.adapters.HTTPAdapter, 'send', new=respond):
            links = [{'target_url': 'https://cdn.test/logo.png', 'target_status': None}]
            crawler._head_check_links(links)
            self.assertEqual(calls, [('HEAD', 'https://cdn.test/logo.png'),
                                     ('GET', 'https://cdn.test/logo.png')])
            self.assertEqual(links[0]['target_status'], 200)
            self.assertTrue(settings.save_settings({'enableLinkChecks': False})[0])
            crawler.update_config(settings.get_crawler_config())
            crawler._head_check_links([{'target_url': 'https://other.test/unchecked', 'target_status': None}])
            self.assertEqual(len(calls), 2)
        self.assertFalse(settings.save_settings({'enableLinkChecks': 'false'})[0])

    def test_private_entrypoint_requires_service_key_and_refuses_browser_work(self):
        with patch.dict(os.environ, {'LIBRECRAWL_API_KEY': 'fixture-service-key',
                                     'LIBRECRAWL_PRIVATE_SERVICE': 'true'}):
            service = importlib.import_module('service')
            client = service.app.test_client()
            self.assertEqual(client.get('/healthz').status_code, 200)
            with patch.dict(os.environ, {'RAILWAY_GIT_COMMIT_SHA': 'a' * 40}):
                self.assertEqual(client.get('/healthz').get_json()['producerRevision'], 'a' * 40)
            self.assertEqual(client.get('/api/get_settings').status_code, 401)
            headers = {'Authorization': 'Bearer fixture-service-key'}
            self.assertEqual(client.get('/', headers=headers).status_code, 404)
            self.assertEqual(client.get('/api/get_settings', headers=headers).status_code, 200)
            self.assertEqual(client.get('/healthz').get_json()['requestPolicy']['userAgent'], HTTP_USER_AGENT)
            for settings in ({'userAgent': 'Mozilla/5.0'}, {'respectRobotsTxt': False},
                             {'enableProxy': True}, {'proxyUrl': 'http://proxy.invalid'},
                             {'customHeaders': 'User-Agent: Mozilla/5.0'}, {'enableLinkChecks': True},
                             {'enableLinkChecks': 'false'}, {'enableLinkChecks': 0}):
                with self.subTest(settings=settings):
                    self.assertEqual(client.post('/api/save_settings', headers=headers, json=settings).status_code, 400)
            # Upstream defaults are not an admitted acquisition budget.
            with patch.object(WebCrawler, 'start_crawl') as start:
                rejected = client.post('/api/start_crawl', headers=headers,
                                       json={'url': 'https://example.test'})
                self.assertEqual(rejected.status_code, 400)
                self.assertIn('max_urls', rejected.get_json()['error'])
                start.assert_not_called()
            rejected = client.post('/api/save_settings', headers=headers,
                                   json={'enableJavaScript': True})
            self.assertEqual(rejected.status_code, 400)
            old_browser_crawl = crawl_db.create_crawl(None, 'old', 'https://example.test',
                                                     'example.test', {'enable_javascript': True})
            self.assertEqual(client.post(f'/api/crawls/{old_browser_crawl}/resume',
                                         headers=headers).status_code, 400)
            invalid_saved = crawl_db.create_crawl(None, 'old', 'https://example.test',
                                                  'example.test', {'max_urls': 100,
                                                  'max_depth': 2, 'max_file_size': 0})
            rejected = client.post(f'/api/crawls/{invalid_saved}/resume', headers=headers)
            self.assertEqual(rejected.status_code, 400)
            self.assertIn('max_file_size', rejected.get_json()['error'])
            bounded = WebCrawler()._get_default_config()
            bounded['max_file_size'] = 1024 * 1024
            for overrides in ({'user_agent': 'Mozilla/5.0'}, {'respect_robots': False},
                              {'enable_proxy': True}, {'custom_headers': {'User-Agent': 'Spoofed'}},
                              {'enable_link_checks': True}, {'enable_link_checks': 'false'}):
                saved = crawl_db.create_crawl(None, 'old', 'https://example.test',
                                              'example.test', {**bounded, **overrides})
                with self.subTest(saved=overrides):
                    self.assertEqual(client.post(f'/api/crawls/{saved}/resume', headers=headers).status_code, 400)
            legacy = {key: value for key, value in bounded.items() if key != 'enable_link_checks'}
            self.assertIsNone(service.capture_policy_error(legacy))
            for value in (0, float('nan'), float('inf'), 11 * 1024 * 1024):
                self.assertIsNotNone(service.capture_limit_error(
                    {'max_urls': 100, 'max_depth': 2, 'max_file_size': value}))
            accepted = client.post('/api/save_settings', headers=headers,
                                   json={'maxUrls': 100, 'maxDepth': 2, 'maxFileSize': 2})
            self.assertTrue(accepted.get_json()['success'])
            with patch.object(WebCrawler, 'start_crawl', return_value=(False, 'fixture: no dispatch')) as start:
                admitted = client.post('/api/start_crawl', headers=headers,
                                       json={'url': 'https://example.test'})
                self.assertEqual(admitted.status_code, 200)
                start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
