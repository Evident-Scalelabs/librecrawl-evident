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
            self.assertEqual(crawler.session.get('https://missing.test/page').status_code, 200)
            allowed = crawler._crawl_url_with_requests(crawler.base_url + '/public', 0)
            self.assertEqual(allowed['status_code'], 200)
            self.assertEqual(crawler.session.get('https://policy-redirect.test/page').status_code, 200)
            with self.assertRaises(RobotsAccessDenied):
                crawler.session.get('https://policy-redirect.test/private')
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

    def test_private_entrypoint_requires_service_key_and_refuses_browser_work(self):
        with patch.dict(os.environ, {'LIBRECRAWL_API_KEY': 'fixture-service-key'}):
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
                             {'customHeaders': 'User-Agent: Mozilla/5.0'}):
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
                              {'enable_proxy': True}, {'custom_headers': {'User-Agent': 'Spoofed'}}):
                saved = crawl_db.create_crawl(None, 'old', 'https://example.test',
                                              'example.test', {**bounded, **overrides})
                with self.subTest(saved=overrides):
                    self.assertEqual(client.post(f'/api/crawls/{saved}/resume', headers=headers).status_code, 400)
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
