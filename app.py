"""Authenticated CareerOps dashboard. Run one Gunicorn worker with threads on Render."""
from datetime import timedelta
from functools import wraps
import hmac
import io
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from pypdf import PdfReader
from core import ROOT, Store, STATUSES, now, later, run_scan

LOGIN = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in | CareerOps</title><link rel="stylesheet" href="/static/login.css"></head><body><main><a class="brand" href="/">C<span>CareerOps</span></a><p class="eyebrow">YOUR NEXT CHAPTER</p><h1>Your search.<br>Your control.</h1><p class="intro">Sign in to your private job-search workspace.</p><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label for="password">Workspace password</label><input id="password" type="password" name="password" autocomplete="current-password" required autofocus><button type="submit">Open workspace <span aria-hidden="true">↗</span></button>{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}</form><p class="footnote">Preparation runs automatically. Applications stay in your hands.</p></main><aside><span>CAREER OPERATIONS / 01</span><h2>Build a focused<br>job-search rhythm.</h2><p>Discover · Match · Prepare · Track</p><div class="orbit"></div></aside></body></html>'''


def create_app(root=ROOT, testing=False, preview=False, start_scheduler=True, database_url=None):
    root = Path(root)
    hosted = os.environ.get('RENDER') == 'true' or bool(os.environ.get('RENDER_SERVICE_ID'))
    if hosted and preview:
        raise RuntimeError('Local preview authentication cannot be enabled on Render')
    if hosted and not (database_url or os.environ.get('DATABASE_URL')):
        raise RuntimeError('DATABASE_URL is required on Render so application history survives restarts')
    password = os.environ.get('DASHBOARD_PASSWORD', '')
    password_hash = os.environ.get('DASHBOARD_PASSWORD_HASH') or (generate_password_hash(password) if password else '')
    secret = os.environ.get('SECRET_KEY', '')
    if not testing and not preview and (not password_hash or len(secret) < 32):
        raise RuntimeError('Set DASHBOARD_PASSWORD (or hash) and a SECRET_KEY of at least 32 characters')
    app = Flask(__name__, static_folder=str(root / 'static'))
    app.config.update(TESTING=testing, SECRET_KEY=secret or secrets.token_hex(32), MAX_CONTENT_LENGTH=5*1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=hosted, PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1) if hosted else app.wsgi_app
    store = Store(root, database_url)
    store.initialize()
    app.extensions['store'] = store
    app.extensions['scheduler_stop'] = threading.Event()
    attempts = {}

    def allowed():
        return bool(session.get('authenticated') or (preview and request.remote_addr in ('127.0.0.1', '::1')))

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_hex(24)
        return session['csrf']

    def protected(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if not allowed():
                return (jsonify(error='Sign in to continue'), 401) if request.path.startswith('/api/') else redirect('/login')
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                supplied = request.headers.get('X-CSRF-Token', '')
                if not supplied or not hmac.compare_digest(supplied, csrf()):
                    return jsonify(error='Refresh the page and retry (CSRF token missing or expired)'), 403
                origin = request.headers.get('Origin')
                if origin and origin != request.host_url.rstrip('/'):
                    return jsonify(error='Cross-origin action rejected'), 403
            return function(*args, **kwargs)
        return wrapped

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        if hosted:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error='File exceeds the 5 MB limit'), 413

    @app.get('/healthz')
    @app.get('/health')
    def health():
        # Uptime monitoring must never launch a scan or disclose private data.
        return jsonify(status='ok', service='careerops')

    @app.get('/readyz')
    def ready():
        try:
            with store.db() as db:
                db.execute('SELECT 1').fetchone()
            return jsonify(status='ready')
        except Exception:
            return jsonify(status='unavailable'), 503

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if allowed():
            return redirect('/')
        error = None
        if request.method == 'POST':
            current = time.monotonic()
            peer = request.remote_addr or 'unknown'
            recent = [value for value in attempts.get(peer, []) if current-value < 300]
            attempts[peer] = recent
            if len(recent) >= 10:
                return render_template_string(LOGIN, csrf=csrf(), error='Too many attempts. Try again in five minutes.'), 429
            submitted_csrf = request.form.get('csrf_token', '')
            if not submitted_csrf or not hmac.compare_digest(submitted_csrf, csrf()):
                return render_template_string(LOGIN, csrf=csrf(), error='Refresh and try again.'), 403
            if password_hash and check_password_hash(password_hash, request.form.get('password', '')):
                session.clear()
                session['authenticated'] = True
                session.permanent = True
                csrf()
                attempts.pop(peer, None)
                return redirect('/')
            recent.append(current)
            error = 'That password did not match.'
        return render_template_string(LOGIN, csrf=csrf(), error=error)

    @app.get('/')
    @protected
    def index():
        return send_file(root / 'static' / 'index.html')

    @app.get('/api/csrf')
    @protected
    def csrf_api():
        return jsonify(csrf_token=csrf())

    @app.post('/api/logout')
    @protected
    def logout():
        session.clear()
        return jsonify(ok=True)

    def bootstrap_data():
        settings = store.setting('automation')
        settings.update(mode='in-process', storage=store.kind, cloud_ready=bool(hosted and store.kind == 'postgres'), health_url=request.host_url.rstrip('/')+'/healthz')
        profile = store.setting('profile')
        profile['resume_available'] = bool(store.document('resume'))
        with store.db() as db:
            sources = [dict(row) for row in db.execute('SELECT * FROM sources ORDER BY source').fetchall()]
            runs = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 30').fetchall()]
        for run in runs:
            run['summary'] = json.loads(run['summary']) if run['summary'] else None
        return {'profile': profile, 'jobs': store.jobs(), 'sources': sources, 'runs': runs, 'automation': settings, 'csrf_token': csrf()}

    @app.get('/api/bootstrap')
    @protected
    def bootstrap():
        return jsonify(bootstrap_data())

    def start_scan():
        token = store.acquire()
        if not token:
            return None
        run_id = uuid.uuid4().hex
        with store.db() as db:
            # A stopped instance can leave an old running row. The lease makes recovery explicit.
            db.execute("UPDATE runs SET status='interrupted',finished_at=?,error='Previous process stopped before completing' WHERE status='running'", (now(),))
            db.execute('INSERT INTO runs(id,started_at,status) VALUES (?,?,?)', (run_id, now(), 'running'))
        def worker():
            try:
                run_scan(store, run_id, token)
            except Exception:
                app.logger.exception('Job scan failed')
        threading.Thread(target=worker, daemon=True, name='careerops-scan').start()
        return run_id
    app.extensions['start_scan'] = start_scan

    @app.post('/api/run')
    @protected
    def scan_api():
        run_id = start_scan()
        return (jsonify(ok=True, run_id=run_id), 202) if run_id else (jsonify(error='A scan is already running'), 409)

    @app.post('/api/automation/run')
    def external_scan():
        token = os.environ.get('AUTOMATION_TOKEN', '')
        supplied = request.headers.get('Authorization', '')
        if len(token) < 32 or not hmac.compare_digest(supplied, 'Bearer '+token):
            return jsonify(error='Unauthorized'), 401
        run_id = start_scan()
        return (jsonify(ok=True, run_id=run_id), 202) if run_id else (jsonify(error='A scan is already running'), 409)

    @app.post('/api/jobs/review')
    @protected
    def review_api():
        data = request.get_json() or {}
        if not isinstance(data.get('key'), str):
            raise ValueError('A job key is required')
        confirmation_url = data.get('confirmation_url', '')
        if confirmation_url and (urlparse(confirmation_url).scheme != 'https' or not urlparse(confirmation_url).netloc):
            raise ValueError('Confirmation link must be an HTTPS URL')
        review = store.review(data['key'], data.get('status'), data, confirmed=data.get('confirmed') is True or data.get('confirmed_applied') is True)
        return jsonify(ok=True, review=review)

    @app.post('/api/jobs/packet')
    @protected
    def packet_api():
        data = request.get_json() or {}
        return jsonify(store.packet(data.get('key')))

    @app.post('/api/settings')
    @protected
    def settings_api():
        data = request.get_json() or {}
        if data.get('submission_mode', 'prepare') != 'prepare':
            raise ValueError('This workspace prepares applications; submission is performed by you.')
        interval = data.get('interval_hours', 1)
        if type(interval) not in (int, float) or interval not in (1, 3, 6, 12, 24):
            raise ValueError('Choose a scan interval of 1, 3, 6, 12 or 24 hours')
        settings = store.setting('automation')
        settings.update(enabled=bool(data.get('enabled', settings['enabled'])), interval_hours=interval, submission_mode='prepare', next_run=later(hours=interval))
        store.save_setting('automation', settings)
        return jsonify(ok=True, automation=settings)

    @app.post('/api/profile')
    @protected
    def profile_api():
        data = request.get_json() or {}
        profile = store.setting('profile')
        for field in ('display_name', 'evidence_for_outreach'):
            if field in data:
                if not isinstance(data[field], str) or len(data[field]) > 5000:
                    raise ValueError('Invalid profile text')
                profile[field] = data[field]
        if 'experience_years' in data:
            value = data['experience_years']
            if type(value) not in (int, float) or not 0 <= value <= 50:
                raise ValueError('Experience must be a number between 0 and 50')
            profile['experience_years'] = value
        if 'resume_facts' in data:
            if not isinstance(data['resume_facts'], list) or len(data['resume_facts']) > 30 or any(not isinstance(fact, str) or len(fact)>2000 for fact in data['resume_facts']):
                raise ValueError('Invalid resume achievements')
            profile['resume_facts'] = data['resume_facts']
        store.save_setting('profile', profile)
        store.invalidate_packets()
        return jsonify(ok=True)

    @app.get('/api/resume')
    @protected
    def resume_download():
        document = store.document('resume')
        if not document:
            return jsonify(error='Upload a resume first'), 404
        return send_file(io.BytesIO(bytes(document['content'])), mimetype='application/pdf', as_attachment=True, download_name='resume.pdf')

    @app.post('/api/resume')
    @protected
    def resume_upload():
        upload = request.files.get('file')
        if not upload:
            raise ValueError('Choose a PDF file')
        content = upload.read()
        if not content.startswith(b'%PDF-'):
            raise ValueError('The file must be a PDF')
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted or len(reader.pages) > 20:
                raise ValueError('Use an unencrypted resume of at most 20 pages')
            text = '\n'.join(page.extract_text() or '' for page in reader.pages)
            if len(text.strip()) < 40:
                raise ValueError('This PDF has no readable text; use a text-based PDF')
        except ValueError:
            raise
        except Exception:
            raise ValueError('The PDF could not be read')
        store.save_document('resume', content, 'application/pdf')
        store.save_setting('resume_text', text[:100000])
        store.invalidate_packets()
        return jsonify(ok=True, resume_available=True, notice='Resume saved privately. Confirm your profile achievements before preparing new drafts.')

    @app.get('/api/export')
    @protected
    def export():
        data = bootstrap_data()
        data.pop('csrf_token', None)
        with store.db() as db:
            data['events'] = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY at').fetchall()]
        return Response(json.dumps(data, indent=2, ensure_ascii=False), mimetype='application/json', headers={'Content-Disposition': 'attachment; filename=careerops-export.json'})

    if start_scheduler and not testing:
        def scheduler():
            stop = app.extensions['scheduler_stop']
            while not stop.wait(30):
                try:
                    settings = store.setting('automation')
                    if settings['enabled'] and settings['next_run'] <= now():
                        start_scan()
                except Exception:
                    app.logger.exception('Scheduler check failed')
        threading.Thread(target=scheduler, daemon=True, name='careerops-scheduler').start()
    return app


# The production entry point refuses ephemeral storage or missing auth on Render.
if os.environ.get('CAREEROPS_FACTORY_ONLY') != '1':
    app = create_app(preview=os.environ.get('LOCAL_PREVIEW') == '1')

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '8765')), debug=False, threaded=True)
