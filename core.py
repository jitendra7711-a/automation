"""CareerOps: public job collection, durable state, and truthful local preparation."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from discovery import unique_jobs

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
STATUSES = ('new', 'shortlisted', 'prepared', 'applied', 'contacted', 'replied', 'interviewing', 'rejected', 'offer', 'withdrawn')


def now():
    return datetime.now(IST).isoformat(timespec='seconds')


def later(hours=0, minutes=0):
    return (datetime.now(IST) + timedelta(hours=hours, minutes=minutes)).isoformat(timespec='seconds')


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Store:
    def __init__(self, root=ROOT, database_url=None):
        self.root = Path(root)
        self.url = database_url if database_url is not None else os.environ.get('DATABASE_URL', '')
        self.kind = 'postgres' if self.url else 'sqlite'
        self.path = self.root / 'data' / 'careerops.sqlite3'
        if not self.url:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db(self, direct=False):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row
            connection_url = (os.environ.get('DATABASE_URL_UNPOOLED') or self.url) if direct else self.url
            connection = psycopg.connect(connection_url, row_factory=dict_row, connect_timeout=15, prepare_threshold=None)
        else:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA journal_mode=WAL')
        class Query:
            def execute(inner, sql, params=()):
                return connection.execute(sql.replace('?', '%s') if self.url else sql, params)
        try:
            yield Query()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        blob = 'BYTEA' if self.url else 'BLOB'
        schema = [
            'CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, source TEXT NOT NULL, payload TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, availability TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS reviews (key TEXT PRIMARY KEY, document TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS packets (key TEXT PRIMARY KEY, document TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, at TEXT NOT NULL, success INTEGER NOT NULL, job_count INTEGER, error TEXT, last_success_at TEXT)',
            'CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, summary TEXT, error TEXT)',
            'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
            f'CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, content {blob} NOT NULL, content_type TEXT NOT NULL, digest TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, key TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, notes TEXT, confirmation_url TEXT)'
        ]
        with self.db(direct=True) as db:
            for sql in schema:
                db.execute(sql)
            profile = load_json(self.root / 'profile.json')
            initial_profile = os.environ.get('INITIAL_PROFILE_JSON', '')
            if initial_profile:
                private_profile = json.loads(initial_profile)
                if not isinstance(private_profile, dict):
                    raise ValueError('INITIAL_PROFILE_JSON must contain a profile object')
                profile.update(private_profile)
            settings = {'enabled': True, 'interval_hours': 1, 'submission_mode': 'prepare', 'next_run': now(), 'last_run': None}
            for key, value in [('profile', profile), ('automation', settings)]:
                db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING', (key, json.dumps(value)))
            db.execute("INSERT INTO leases VALUES ('scan','','') ON CONFLICT(name) DO NOTHING")
        resume = self.root / 'private' / 'resume.pdf'
        if resume.exists() and self.document('resume', metadata=True) is None:
            self.save_document('resume', resume.read_bytes(), 'application/pdf')

    def setting(self, key):
        with self.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None

    def save_setting(self, key, value):
        with self.db() as db:
            db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def invalidate_packets(self):
        with self.db() as db:
            db.execute('DELETE FROM packets')
            rows = db.execute('SELECT * FROM reviews').fetchall()
            for row in rows:
                review = json.loads(row['document'])
                if review.get('status') == 'prepared':
                    review['status'] = 'shortlisted'
                    db.execute('UPDATE reviews SET document=? WHERE key=?', (json.dumps(review), row['key']))

    def document(self, name, metadata=False):
        with self.db() as db:
            columns = 'name,content_type,digest' if metadata else '*'
            row = db.execute(f'SELECT {columns} FROM documents WHERE name=?', (name,)).fetchone()
        return dict(row) if row else None

    def save_document(self, name, content, content_type):
        digest = hashlib.sha256(content).hexdigest()
        with self.db() as db:
            db.execute('INSERT INTO documents VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET content=excluded.content,content_type=excluded.content_type,digest=excluded.digest', (name, content, content_type, digest))
        return digest

    def acquire(self):
        token = secrets.token_hex(20)
        with self.db() as db:
            result = db.execute("UPDATE leases SET token=?,expires_at=? WHERE name='scan' AND expires_at<=?", (token, later(minutes=30), now()))
            return token if result.rowcount == 1 else None

    def release(self, token):
        with self.db() as db:
            db.execute("UPDATE leases SET token='',expires_at='' WHERE name='scan' AND token=?", (token,))

    def source_result(self, source, jobs=None, error=None, incremental=False):
        timestamp = now()
        with self.db() as db:
            if jobs is not None:
                if not incremental:
                    db.execute("UPDATE jobs SET availability='not_in_latest_feed' WHERE source=?", (source,))
                for job in jobs:
                    db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,'active') ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen,availability='active'", (job['key'], source, json.dumps(job, ensure_ascii=False), timestamp, timestamp))
                db.execute('INSERT INTO sources VALUES (?,?,1,?,NULL,?) ON CONFLICT(source) DO UPDATE SET at=excluded.at,success=1,job_count=excluded.job_count,error=NULL,last_success_at=excluded.last_success_at', (source, timestamp, len(jobs), timestamp))
            else:
                db.execute('INSERT INTO sources VALUES (?,?,0,NULL,?,NULL) ON CONFLICT(source) DO UPDATE SET at=excluded.at,success=0,error=excluded.error', (source, timestamp, str(error)[:500]))

    def jobs(self):
        profile = self.setting('profile')
        with self.db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE availability='active' OR key IN (SELECT key FROM reviews)").fetchall()
            reviews = {row['key']: json.loads(row['document']) for row in db.execute('SELECT key,document FROM reviews').fetchall()}
            packets = {row['key']: json.loads(row['document']) for row in db.execute('SELECT key,document FROM packets').fetchall()}
        result = []
        for row in rows:
            job = json.loads(row['payload'])
            assessment = assess(job, profile)
            if not assessment['relevant'] and row['key'] not in reviews:
                continue
            job.update(first_seen=row['first_seen'], last_seen=row['last_seen'], availability=row['availability'], assessment=assessment, review=reviews.get(row['key'], {'status': 'new'}), packet=packets.get(row['key']))
            result.append(job)
        return unique_jobs(result)

    def job(self, key):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
            if row is None:
                return None
            review = db.execute('SELECT document FROM reviews WHERE key=?', (key,)).fetchone()
            packet = db.execute('SELECT document FROM packets WHERE key=?', (key,)).fetchone()
        job = json.loads(row['payload'])
        job.update(first_seen=row['first_seen'], last_seen=row['last_seen'], availability=row['availability'], assessment=assess(job, self.setting('profile')), review=json.loads(review['document']) if review else {'status': 'new'}, packet=json.loads(packet['document']) if packet else None)
        return job

    def review(self, key, status, fields=None, confirmed=False):
        fields = fields or {}
        if status not in STATUSES:
            raise ValueError('Unknown status')
        job = self.job(key)
        if job is None:
            raise ValueError('Unknown job')
        review = job['review'].copy()
        if status == 'applied' and not confirmed:
            raise ValueError('Confirm that you actually submitted the employer application before marking Applied.')
        if status == 'prepared' and not job.get('packet'):
            raise ValueError('Prepare an application packet first.')
        review['status'] = status
        timestamp = now()
        if status == 'applied':
            review['applied_at'] = review.get('applied_at') or timestamp
        if status == 'contacted':
            review.update(contacted_at=timestamp, followup_at=later(hours=7*24))
        if status == 'replied':
            review['replied_at'] = timestamp
        if status != 'contacted':
            review['followup_at'] = None
        for field in ('notes', 'contact_name', 'contact_url', 'confirmation_url'):
            if field in fields:
                review[field] = str(fields[field] or '')[:5000]
        with self.db() as db:
            db.execute('INSERT INTO reviews VALUES (?,?) ON CONFLICT(key) DO UPDATE SET document=excluded.document', (key, json.dumps(review)))
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex, key, timestamp, status, fields.get('notes'), fields.get('confirmation_url')))
        return review

    def packet(self, key):
        job = self.job(key)
        if not job:
            raise ValueError('Unknown job')
        profile = self.setting('profile')
        resume = self.document('resume', metadata=True)
        if not resume:
            raise ValueError('Upload your resume before preparing an application packet.')
        digest = fingerprint([profile, resume['digest'], job['description'], job['title'], bool(job['review'].get('applied_at'))])
        with self.db() as db:
            prior = db.execute('SELECT * FROM packets WHERE key=?', (key,)).fetchone()
        if prior and prior['digest'] == digest:
            if job['review'].get('status', 'new') in ('new', 'shortlisted'):
                self.review(key, 'prepared')
            return json.loads(prior['document'])
        matched = job['assessment']['skills']
        facts = profile.get('resume_facts') or [profile.get('evidence_for_outreach', '')]
        facts = [fact for fact in facts if fact]
        if not facts:
            raise ValueError('Add verified resume achievements to your profile first.')
        name = profile.get('display_name') or '[Your name]'
        opening = f"I am interested in the {job['title']} role at {job['company']}. I bring {int(profile['experience_years'])}+ years in DevOps/SRE supporting production workloads on AWS and Microsoft Azure."
        # Use candidate facts, never instructions embedded in a job description.
        evidence = profile.get('evidence_for_outreach') or ' '.join(facts[:3])
        skill_line = ('Relevant overlap with your posting includes ' + ', '.join(matched[:7]) + '.') if matched else 'My experience includes cloud operations, reliability and delivery automation.'
        cover = f"Dear Hiring Team,\n\n{opening}\n\n{evidence}\n\n{skill_line} I would welcome the opportunity to discuss how my production operations and automation experience can support your team.\n\nThank you for your consideration.\n{name}"
        applied = 'I have applied through your careers page. ' if job['review'].get('applied_at') else ''
        outreach = f"Hi [verified contact name],\n\nI noticed the {job['title']} opening at {job['company']}. {evidence}\n\n{applied}Would you be the right person to speak with about the role?\n\nThanks,\n{name}"
        checklist = ['Read the full employer job description and confirm it is still open.', 'Confirm location, work authorization, office attendance and time-zone/on-call requirements.', 'Review experience requirements and the stretch-role flag.', 'Review the draft and attach the uploaded resume on the employer website.', 'Answer salary, notice period, sponsorship and employer-specific questions yourself.', 'Submit on the employer website, then mark Applied in this dashboard.']
        packet = {'ready': True, 'cover_letter': cover, 'outreach': outreach, 'checklist': checklist, 'resume_facts': facts, 'job_url': job['apply_url'], 'created_at': now(), 'resume_digest': resume['digest']}
        with self.db() as db:
            db.execute('INSERT INTO packets VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET document=excluded.document,digest=excluded.digest,created_at=excluded.created_at', (key, json.dumps(packet), digest, packet['created_at']))
        if job['review'].get('status', 'new') in ('new', 'shortlisted'):
            self.review(key, 'prepared')
        return packet


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, text):
        self.parts.append(text)


def plain(value):
    parser = TextParser()
    parser.feed(html.unescape(str(value or '')))
    return re.sub(r'\s+', ' ', ' '.join(parser.parts)).strip()


def has_term(text, term):
    return bool(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.I))


def experience_evidence(description):
    pattern = r'\b(\d{1,2})(?:\s*(?:-|\u2013|\u2014|to)\s*(\d{1,2}))?\s*\+?\s*(?:years?|yrs?)\b'
    found = []
    for match in re.finditer(pattern, description, re.I):
        tail = description[match.end():match.end()+150]
        explicit = re.match(r'\s+(?:of\s+)?[^.;:!?]{0,90}?\bexperience\b', tail, re.I)
        domain = re.match(r'\s+(?:of\s+)?(?:hands.on\s+)?(?:in|building|operating|managing|developing|designing|supporting|running)\b[^.;:!?]{0,120}', tail, re.I)
        valid_domain = domain and re.search(r'\b(devops|sre|infrastructure|platform|reliability|cloud|gpu|systems|software)\b', domain.group(0), re.I)
        if explicit or valid_domain:
            item = (int(match.group(1)), (match.group(0) + (explicit.group(0) if explicit else domain.group(0))).strip())
            if item not in found:
                found.append(item)
    return found


def assess(job, profile):
    role = any(has_term(job['title'], term) for term in profile['target_roles'])
    location = job['location']
    india = any(has_term(location, term) for term in profile['location_terms']) or bool(re.search(r'\b(?:IN|IND)\b', location))
    global_hint = any(has_term(location, term) for term in ('worldwide', 'anywhere', 'global', 'apac', 'asia'))
    vague = not location or (job['workplace'] == 'remote' and location.strip().lower() == 'remote')
    evidence = experience_evidence(job['description'])
    required = max((minimum for minimum, _ in evidence), default=None)
    senior = bool(re.search(r'\b(senior|sr\.?|staff|principal|lead|manager|director|head)\b', job['title'], re.I))
    tier = 'Stretch: experience requirement' if required is not None and required > profile['experience_years'] else 'Stretch: senior title' if senior else 'Review: experience not parsed' if required is None else 'Priority review'
    skills = [name for name, terms in profile['skills'].items() if any(has_term(job['description'], term) for term in terms)]
    score = max(0, min(100, round((40 if role else 0) + (20 if india else 5) + min(40, 5*len(skills)) - (20 if tier.startswith('Stretch') else 0))))
    # The default discovery queue is DevOps/SRE at entry / two-year level.
    # Unknown requirements remain available in a separate review queue.
    target_role = any(has_term(job['title'], term) for term in ('devops', 'dev ops', 'dev-ops', 'site reliability', 'sre'))
    entry = bool(target_role and not senior and required is not None and required <= min(2, profile['experience_years']))
    relevant = bool(role and (india or global_hint or vague) and job['workplace'] in profile['work_modes'])
    return {'relevant': relevant, 'entry_suitable': bool(relevant and entry), 'score': score, 'tier': tier, 'geography': 'India listed; verify restrictions' if india else 'Location eligibility needs review', 'skills': skills, 'experience_evidence': [text for _, text in evidence], 'minimum_years_hint': required}


def feed_url(company):
    board = company['board']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', board):
        raise ValueError('Invalid board identifier')
    provider = company['provider']
    if provider == 'jobicy':
        return 'https://jobicy.com/api/jobs?count=200&tag='+urllib.parse.quote(board)
    if provider == 'greenhouse':
        return f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true'
    if provider == 'lever':
        host = 'api.eu.lever.co' if company.get('region') == 'eu' else 'api.lever.co'
        return f'https://{host}/v0/postings/{board}?mode=json'
    if provider == 'ashby':
        return f'https://api.ashbyhq.com/posting-api/job-board/{board}'
    raise ValueError('Unsupported source')


def fetch(company):
    url = feed_url(company)
    if company['provider'] == 'jobicy':
        jobs, cursors = [], set()
        for page in range(20):
            data = fetch_json(url)
            if data.get('success') is False or not isinstance(data.get('jobs'), list):
                raise ValueError('Jobicy feed schema changed')
            jobs.extend(data['jobs'])
            cursor = data.get('nextCursor')
            if not cursor:
                return {'jobs': jobs}
            if cursor in cursors:
                raise ValueError('Repeated pagination cursor')
            cursors.add(cursor)
            url = feed_url(company)+'&cursor='+urllib.parse.quote(cursor, safe='')
        raise ValueError('Feed exceeded pagination limit; previous snapshot retained')
    return fetch_json(url)


def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'CareerOps/1.1 (personal job monitor)', 'Accept': 'application/json'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                data = response.read(20_000_001)
            if len(data) > 20_000_000:
                raise ValueError('Feed exceeded size limit')
            return json.loads(data)
        except urllib.error.HTTPError as error:
            if attempt == 2 or error.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(2 ** (attempt+1))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt+1))


def normalize(company, data):
    provider = company['provider']
    jobs = data if provider == 'lever' else data.get('jobs') if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError('Feed schema changed; previous snapshot retained')
    result = []
    for raw in jobs:
        if not isinstance(raw, dict):
            raise ValueError('Invalid job record')
        if provider == 'ashby' and raw.get('isListed') is False:
            continue
        if provider == 'greenhouse':
            title, url = raw.get('title'), raw.get('absolute_url')
            location = (raw.get('location') or {}).get('name', '')
            description = plain(raw.get('content'))
            workplace, published = 'unknown', raw.get('first_published', '')
        elif provider == 'lever':
            title, url = raw.get('text'), raw.get('hostedUrl')
            categories = raw.get('categories') or {}
            location = '; '.join(categories.get('allLocations') or [categories.get('location', '')]) + ' ' + str(raw.get('country') or '')
            description = plain(raw.get('descriptionPlain') or raw.get('descriptionBodyPlain') or raw.get('description'))
            description += ' ' + ' '.join(plain(x.get('content')) for x in raw.get('lists', []))
            description += ' ' + plain(raw.get('additionalPlain') or raw.get('additional'))
            workplace = raw.get('workplaceType', 'unknown')
            published = datetime.fromtimestamp(raw['createdAt']/1000, IST).isoformat() if raw.get('createdAt') else ''
        elif provider == 'jobicy':
            title, url = raw.get('jobTitle'), raw.get('url')
            location, description = raw.get('jobGeo', ''), plain(raw.get('jobDescription') or raw.get('jobExcerpt'))
            workplace, published = 'remote', raw.get('pubDate', '')
        else:
            title, url = raw.get('title'), raw.get('jobUrl')
            locations = [raw.get('location', ''), ((raw.get('address') or {}).get('postalAddress') or {}).get('addressCountry', '')]
            for secondary in raw.get('secondaryLocations') or []:
                locations.extend([secondary.get('location', ''), (secondary.get('address') or {}).get('addressCountry', '')])
            location = '; '.join(str(x) for x in locations if x)
            description = plain(raw.get('descriptionPlain') or raw.get('descriptionHtml'))
            # isRemote also covers hybrid on Ashby; workplaceType is authoritative.
            workplace, published = raw.get('workplaceType', 'unknown'), raw.get('publishedAt', '')
        if not title or not url or urllib.parse.urlparse(url).scheme != 'https':
            raise ValueError('Missing title/HTTPS URL; previous snapshot retained')
        workplace = str(workplace or 'unknown').lower().replace('-', '')
        if workplace not in ('remote', 'hybrid', 'onsite'):
            workplace = next((mode for mode in ('hybrid', 'onsite', 'remote') if has_term(f'{title} {location}', mode)), 'unknown')
        source = f"{provider}:{company['board']}"
        identifier = str(raw.get('id') or hashlib.sha256(url.encode()).hexdigest()[:24])
        apply_url = raw.get('applyUrl') or url
        if urllib.parse.urlparse(apply_url).scheme != 'https':
            apply_url = url
        result.append({'key': f'{source}:{identifier}', 'source': source, 'company': raw.get('companyName', company['name']) if provider == 'jobicy' else company['name'], 'title': title.strip(), 'location': location.strip(), 'description': description.strip(), 'workplace': workplace, 'url': url, 'apply_url': apply_url, 'published_at': published, 'date_kind': 'created' if provider == 'lever' else 'published', 'updated_at': raw.get('updated_at', ''), 'funding_verified': False})
    if len({job['key'] for job in result}) != len(result):
        raise ValueError('Duplicate identifiers in feed')
    return result


def run_scan(store, run_id, token):
    failures, total, prepared = [], 0, 0
    try:
        from concurrent.futures import ThreadPoolExecutor
        companies = load_json(store.root / 'companies.json')
        profile = store.setting('profile')
        def collect(company):
            try:
                return company, normalize(company, fetch(company)), None
            except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
                return company, None, error
        with ThreadPoolExecutor(max_workers=4) as pool:
            for company, jobs, error in pool.map(collect, companies):
                source = f"{company['provider']}:{company['board']}"
                if error is None:
                    total += len(jobs)
                    # Keep the database small: persist relevant role/location records,
                    # while source totals in the run report include all screened jobs.
                    store.source_result(source, jobs=[j for j in jobs if assess(j, profile)['relevant']])
                else:
                    store.source_result(source, error=error)
                    failures.append(f'{source}: {error}')
        jobs = store.jobs()
        for job in jobs:
            if job['discovery_ready'] and job['review'].get('status', 'new') in ('new', 'shortlisted', 'prepared'):
                try:
                    store.packet(job['key'])
                    prepared += 1
                except ValueError:
                    pass  # Missing resume is visible in profile and run summary.
        summary = {'openings_collected': total, 'relevant_leads': len(jobs), 'fresh_entry_leads': sum(j['discovery_ready'] for j in jobs), 'packets_prepared': prepared, 'sources_failed': len(failures), 'window_hours': 72}
        with store.db() as db:
            db.execute('UPDATE runs SET finished_at=?,status=?,summary=?,error=? WHERE id=?', (now(), 'partial' if failures else 'completed', json.dumps(summary), '\n'.join(failures) or None, run_id))
    except Exception as error:
        with store.db() as db:
            db.execute('UPDATE runs SET finished_at=?,status=?,error=? WHERE id=?', (now(), 'failed', type(error).__name__, run_id))
        raise
    finally:
        settings = store.setting('automation')
        settings.update(last_run=now(), next_run=later(hours=settings['interval_hours']))
        store.save_setting('automation', settings)
        store.release(token)
