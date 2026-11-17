"""Reconcile explicit portal application receipts, never recommendation cards."""
from datetime import datetime, timedelta
from email.utils import parseaddr
import hashlib
import json
import re
from bs4 import BeautifulSoup, NavigableString, Tag
from discovery import IST, canonical_url, timestamp, words

BOUNDARY = re.compile(r'^(?:now,?\s*take these next steps|view similar jobs|similar jobs for you|recommended jobs)', re.I)
ROLE = re.compile(r'\b(dev.?ops|sre|site reliability|platform engineer|cloud (?:infrastructure )?engineer)\b', re.I)


def parse_receipts(messages, reference=None):
    from alerts import portal_for, job_link, parse_alerts
    reference = reference or datetime.now(IST)
    receipts = []
    for message in messages:
        sender = parseaddr(str(message.get('from', '')))[1].lower().rsplit('@', 1)[-1]
        portal = portal_for(sender)
        received = timestamp(message.get('received_at'))
        subject = str(message.get('subject', ''))
        linkedin = portal == 'linkedin.com' and re.search(r'\byour application was sent to\b', subject, re.I)
        naukri = portal == 'naukri.com' and re.search(r'\byou applied for \d+ jobs?\b', subject, re.I)
        if not (linkedin or naukri) or not received or not timedelta(0) <= reference-received <= timedelta(days=30):
            continue
        soup = BeautifulSoup(str(message.get('html', ''))[:600000], 'html.parser')
        for element in soup(['head', 'style', 'script']):
            element.decompose()
        urls, lines = set(), []
        for element in soup.descendants:
            if isinstance(element, NavigableString):
                text = ' '.join(str(element).split())
                if BOUNDARY.search(text):
                    break
                if text:
                    lines.append(text)
            elif isinstance(element, Tag) and element.name == 'a':
                link = job_link(element.get('href', ''))
                if link and link[0] == portal:
                    if not linkedin or not urls or link[1] in urls:
                        urls.add(link[1])
        # parse_alerts supplies sanitized vacancy fields. Only destinations before
        # the recommendation boundary are evidence of an actual submission.
        candidates = [j for j in parse_alerts([dict(message, subject='', received_at=reference.isoformat())], reference)
                      if j['url'] in urls]
        if naukri and not candidates:
            for index, line in enumerate(lines):
                if ROLE.search(line) and 4 < len(line) < 160 and index+1 < len(lines):
                    company = lines[index+1]
                    if len(company) < 100 and not re.search(r'track|applied|recommend|applications', company, re.I):
                        candidates.append({'title': line, 'company': company, 'url': '', 'source': 'alerts:'+portal})
        # Supported Naukri summaries expose the first submitted card. The
        # subject's total is not enough to identify the other applications.
        for job in candidates[:1] if naukri else candidates:
            if job.get('alert_received_at'):
                job['alert_received_at'] = received.isoformat()
            # No source message ID in older bridge versions: derive a stable key.
            identity = str(message.get('id') or hashlib.sha256((subject+received.isoformat()).encode()).hexdigest())
            receipt_id = hashlib.sha256((identity+'|'+(job.get('url') or words(job['company']+' '+job['title']))).encode()).hexdigest()
            receipts.append({'id': receipt_id, 'portal': portal, 'received_at': received.isoformat(),
                             'url': job.get('url', ''), 'title': job['title'], 'company': job['company'], 'job': job})
    return list({r['id']: r for r in receipts}.values())


def reconcile(store, new_receipts=(), retry=False):
    from core import now, assess
    receipts = {r['id']: r for r in store.setting('submission_receipts') or []}
    incoming = {r['id']: r for r in new_receipts if r['id'] not in receipts}
    receipts.update(incoming)
    retained = sorted(receipts.values(), key=lambda r: r['received_at'], reverse=True)[:1000]
    pending = retained if retry else list(incoming.values())
    # Alert-only batches and repeated receipts must not download every job from
    # the free database. The hourly employer scan retries deferred matches once.
    if not pending:
        return {'receipts_found': len(new_receipts), 'receipts_matched': 0, 'submissions_updated': 0}
    with store.db() as db:
        jobs = {r['key']: json.loads(r['payload']) for r in db.execute('SELECT key,payload FROM jobs').fetchall()}
        reviews = {r['key']: json.loads(r['document']) for r in db.execute('SELECT key,document FROM reviews').fetchall()}
    changed, matched = 0, 0
    for receipt in pending:
        url = canonical_url(receipt.get('url'))
        keys = [key for key, job in jobs.items() if url and url in (canonical_url(job.get('url')), canonical_url(job.get('apply_url')))]
        if not keys:
            possible = [key for key, job in jobs.items() if words(job['company']) == words(receipt['company']) and words(job['title']) == words(receipt['title'])]
            # A title-only receipt cannot select between different requisitions.
            destinations = {canonical_url(jobs[k].get('apply_url') or jobs[k].get('url')) for k in possible}
            if len(destinations) == 1:
                keys = possible
        if not keys and url and receipt.get('job', {}).get('key'):
            job = receipt['job']
            if assess(job, store.setting('profile'))['relevant']:
                store.source_result(job['source'], jobs=[job], incremental=True)
                jobs[job['key']] = job
                keys = [job['key']]
        if keys:
            matched += 1
            receipt['matched_keys'] = keys
            receipt.pop('job', None)
        for key in keys:
            review = reviews.get(key, {'status': 'new'}).copy()
            if review.get('applied_at'):
                continue
            review['applied_at'] = receipt['received_at']
            review['submission_evidence'] = {'kind': 'portal_confirmation_email', 'portal': receipt['portal'], 'received_at': receipt['received_at'], 'receipt_id': receipt['id']}
            if review.get('status', 'new') in ('new', 'shortlisted', 'prepared'):
                review['status'] = 'applied'
            with store.db() as db:
                db.execute('INSERT INTO reviews VALUES (?,?) ON CONFLICT(key) DO UPDATE SET document=excluded.document', (key, json.dumps(review)))
                db.execute('INSERT INTO events VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING', ('receipt:'+receipt['id']+':'+key, key, now(), 'applied', 'Submission confirmed by '+receipt['portal']+' email', receipt.get('url') or ''))
            reviews[key] = review
            changed += 1
    store.save_setting('submission_receipts', retained)
    return {'receipts_found': len(new_receipts), 'receipts_matched': matched, 'submissions_updated': changed}
