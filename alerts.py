"""Read job-alert HTML supplied by the owner, without fetching portal pages."""
from datetime import datetime, timedelta
from email.utils import parseaddr
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from bs4 import BeautifulSoup
from discovery import IST, timestamp

PORTALS = {
    'linkedin.com': 'LinkedIn', 'naukri.com': 'Naukri', 'foundit.in': 'Foundit',
    'indeed.com': 'Indeed', 'indeed.co.in': 'Indeed', 'glassdoor.com': 'Glassdoor',
    'instahyre.com': 'Instahyre', 'cutshort.io': 'Cutshort', 'wellfound.com': 'Wellfound',
    'timesjobs.com': 'TimesJobs', 'hirist.tech': 'Hirist', 'hirist.com': 'Hirist'
}


def portal_for(host):
    return next((domain for domain in PORTALS if host == domain or host.endswith('.'+domain)), None)


def job_link(href):
    try:
        url = urlsplit(href)
        host = (url.hostname or '').lower()
        portal = portal_for(host)
        if url.scheme != 'https' or not portal:
            return None
        path = url.path
        if portal == 'linkedin.com':
            match = re.search(r'/jobs/view/(\d+)', path)
            if not match:
                return None
            return portal, 'https://www.linkedin.com/jobs/view/'+match.group(1), match.group(1)
        # Do not follow email tracking redirects or turn alert/search pages into jobs.
        if not re.search(r'/(?:job-listings|job-listing|job-detail/|job-\d|jobs?/(?!search|alerts)|j/|rc/clk|viewjob)', path, re.I):
            return None
        if re.search(r'(?:search|alert|unsubscribe|preferences)', path, re.I):
            return None
        query = urlencode([(k, v) for k, v in parse_qsl(url.query) if k in ('jk', 'vjk')]) if portal.startswith('indeed.') else ''
        clean = urlunsplit(('https', host, path.rstrip('/'), query, ''))
        return portal, clean, hashlib.sha256(clean.encode()).hexdigest()[:24]
    except ValueError:
        return None


def parse_alerts(messages, reference=None):
    reference = reference or datetime.now(IST)
    jobs = {}
    for message in messages:
        sender = parseaddr(str(message.get('from', '')))[1].lower().rsplit('@', 1)[-1]
        sender_portal = portal_for(sender)
        received = timestamp(message.get('received_at'))
        if not sender_portal or not received or not timedelta(0) <= reference-received <= timedelta(hours=72):
            continue
        # Confirmation messages are tracking evidence, not vacancy advertisements.
        if re.search(r'(application (?:was sent|submitted|received)|you applied|thank you for applying)', message.get('subject', ''), re.I):
            continue
        soup = BeautifulSoup(str(message.get('html', ''))[:600000], 'html.parser')
        for unwanted in soup(['style', 'script', 'head']):
            unwanted.decompose()
        for anchor in soup.find_all('a', href=True):
            link = job_link(anchor['href'])
            if not link or link[0] != sender_portal:
                continue
            pieces = list(anchor.stripped_strings)
            if not pieces:
                continue
            title = pieces[0].strip()
            if len(title) < 5 or len(title) > 180 or re.fullmatch(r'(apply(?: now)?|view(?: job)?|see (?:job|details)|easy apply)', title, re.I):
                continue
            if not re.search(r'\b(dev.?ops|sre|site reliability|platform engineer|cloud (?:infrastructure )?engineer)\b', title, re.I):
                continue
            card = anchor.find_parent('td') or anchor
            lines = list(card.stripped_strings)
            metadata = next((text for text in pieces[1:]+lines if ('·' in text or '•' in text) and len(text) < 240), '')
            company, location = (re.split(r'\s*[·•]\s*', metadata, maxsplit=1)+[''])[:2] if metadata else ('Employer not provided', '')
            if not location:
                # Naukri and other templates often provide location separately.
                location = next((x for x in lines if re.search(r'\b(India|Bengaluru|Bangalore|Mumbai|Pune|Hyderabad|Chennai|Noida|Gurugram)\b', x, re.I) and len(x) < 140), '')
            workplace = next((m for m in ('remote', 'hybrid', 'onsite') if m in location.lower().replace('-', '')), 'unknown')
            portal, url, identifier = link
            key = 'alerts:'+portal+':'+identifier
            # Receipt is never passed off as publication. Full JD / experience
            # and employer publication date require review on the original page.
            job = {'key': key, 'source': 'alerts:'+portal, 'company': company.strip(), 'title': title,
                   'location': location.strip(), 'description': ' '.join(lines)[:12000],
                   'workplace': workplace, 'url': url, 'apply_url': url,
                   'published_at': '', 'date_kind': 'alert_received', 'alert_received_at': received.isoformat(),
                   'updated_at': '', 'funding_verified': False, 'description_complete': False}
            existing = jobs.get(key)
            if not existing or len(job['description']) > len(existing['description']):
                jobs[key] = job
    return list(jobs.values())


def ingest(store, messages, automatic=False):
    from core import now
    jobs = parse_alerts(messages)
    for source in {j['source'] for j in jobs}:
        store.source_result(source, jobs=[j for j in jobs if j['source'] == source], incremental=True)
    status = store.setting('alert_bridge') or {}
    status.update(last_sync=now(), messages_checked=len(messages), jobs_received=len(jobs))
    if automatic:
        status['bridge_connected_at'] = now()
    store.save_setting('alert_bridge', status)
    return {'messages_checked': len(messages), 'jobs_received': len(jobs)}
