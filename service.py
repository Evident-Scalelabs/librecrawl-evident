"""Private HTTP-only entrypoint; keeps LibreCrawl's existing API and crawl owner."""
import os
import math
import secrets
import signal

API_KEY = os.environ.get('LIBRECRAWL_API_KEY', '').strip()
if not API_KEY or not os.environ.get('SECRET_KEY'):
    raise RuntimeError('LIBRECRAWL_API_KEY and a stable SECRET_KEY are required')

# Machine authentication surrounds the upstream local account. No UI or login flow.
os.environ['LOCAL_MODE'] = 'true'
os.environ['REGISTRATION_DISABLED'] = 'true'
os.environ['DISABLE_GUEST'] = 'true'

import main
from flask import jsonify, request, session
from src.crawl_db import get_crawl_by_id, get_db

app = main.app


def capture_limit_error(config):
    # Service ceilings reject the upstream five-million-page UI default.
    for key, ceiling in (('max_urls', 1000), ('max_depth', 10),
                         ('max_file_size', 10 * 1024 * 1024)):
        value = config.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 < value <= ceiling or not math.isfinite(value)
                or (key != 'max_file_size' and not float(value).is_integer())):
            return f'Set {key} to a finite positive value no greater than {ceiling}'
    return None


@app.before_request
def require_service_key():
    if request.path == '/healthz':
        return None
    if not request.path.startswith('/api/') or request.path in (
            '/api/register', '/api/login', '/api/guest-login', '/api/logout'):
        return jsonify({'error': 'Not found'}), 404
    if not secrets.compare_digest(request.headers.get('Authorization', ''), f'Bearer {API_KEY}'):
        return jsonify({'error': 'Service authentication required'}), 401

    if request.path == '/api/save_settings':
        settings = request.get_json(silent=True) or {}
        if settings.get('enableJavaScript') not in (None, False) or settings.get('enablePageSpeed') not in (None, False):
            return jsonify({'error': 'This service only collects HTTP evidence'}), 400
    if request.path in ('/api/start_crawl', '/api/resume_crawl', '/api/update_crawler_settings') or request.path.endswith('/resume'):
        if request.endpoint == 'resume_crawl_endpoint':
            crawl = get_crawl_by_id(request.view_args['crawl_id'])
            if not crawl:
                return None  # The owning route reports a missing crawl.
            config = (crawl or {}).get('config_snapshot') or {}
        elif request.path == '/api/resume_crawl':
            config = main.get_or_create_crawler().config
        else:
            config = main.get_session_settings().get_crawler_config()
        if config.get('enable_javascript') or config.get('enable_pagespeed'):
            return jsonify({'error': 'This crawl requires a renderer or PageSpeed; this service is HTTP-only'}), 400
        error = capture_limit_error(config)
        if error:
            return jsonify({'error': error}), 400


@app.route('/healthz')
def health():
    with get_db() as connection:
        connection.execute('SELECT 1')
    return jsonify({'status': 'ok', 'capture_method': 'http',
                    'producerRevision': os.environ.get('RAILWAY_GIT_COMMIT_SHA')})


if __name__ == '__main__':
    from waitress import serve

    signal.signal(signal.SIGTERM, main.graceful_shutdown)
    signal.signal(signal.SIGINT, main.graceful_shutdown)
    main.recover_crashed_crawls()
    # Bootstrap once before concurrent API calls can race to create this account.
    with app.test_request_context('/'):
        main.auto_login_local_mode()
        if not session.get('user_id'):
            raise RuntimeError('Could not initialize the private service account')
    main.start_cleanup_thread()
    # Waitress explicitly sets IPV6_V6ONLY; wildcard listen binds both families.
    serve(app, listen=f"*:{int(os.environ.get('PORT', '5000'))}", threads=8)
