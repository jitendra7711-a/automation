"""Publication freshness and conservative, history-preserving job deduplication."""
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

IST = timezone(timedelta(hours=5, minutes=30))


def timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except ValueError:
        return None


def freshness(job, reference=None):
    reference = reference or datetime.now(IST)
    published = timestamp(job.get('published_at'))
    verified = job.get('date_kind', 'published') == 'published'
    age = (reference - published).total_seconds() if published else None
    state = 'undated' if not published or not verified else 'future' if age < 0 else 'fresh' if age <= 72*3600 else 'stale'
    return {'state': state, 'hours': round(age, 1) if age is not None else None,
            'day': published.astimezone(IST).date().isoformat() if published and verified else None,
            'window_hours': 72, 'basis': job.get('date_kind', 'published')}


def canonical_url(value):
    try:
        parsed = urlsplit(value or '')
        if parsed.scheme != 'https' or not parsed.hostname:
            return ''
        host = parsed.hostname.lower().removeprefix('www.')
        path = parsed.path.rstrip('/')
        match = re.search(r'/jobs/view/(\d+)', path) if host == 'linkedin.com' else None
        if match:
            path = '/jobs/view/'+match.group(1)
        # Keep requisition query parameters; remove attribution only.
        ignored = {'source', 'gh_src', 'lever-source', 'trk', 'trackingid', 'refid', 'ref', 'referrer'}
        query = sorted((k, v) for k, v in parse_qsl(parsed.query) if not k.lower().startswith('utm_') and k.lower() not in ignored)
        return urlunsplit(('https', host, path, urlencode(query), ''))
    except ValueError:
        return ''


def words(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value).lower()).strip()


def unique_jobs(jobs, reference=None):
    """Exact destination dedup; semantic merges require one unambiguous employer record.

    Different employer requisition IDs are retained, even with the same title.
    Reviews remain on their original keys and take precedence in the display.
    """
    groups, urls = [], {}
    for job in jobs:
        url = canonical_url(job.get('apply_url') or job.get('url'))
        if url and url in urls:
            groups[urls[url]].append(job)
        else:
            if url:
                urls[url] = len(groups)
            groups.append([job])
    # Only link a portal copy when company, title and location all agree and
    # there is exactly one employer requisition. No fuzzy title/location merges.
    semantic = {}
    for index, group in enumerate(groups):
        j = group[0]
        signature = tuple(words(j.get(k, '')) for k in ('company', 'title', 'location'))
        if all(signature):
            semantic.setdefault(signature, []).append(index)
    removed = set()
    for indices in semantic.values():
        employers = [i for i in indices if groups[i][0]['source'].split(':')[0] in ('greenhouse', 'lever', 'ashby')]
        portals = [i for i in indices if groups[i][0]['source'].split(':')[0] not in ('greenhouse', 'lever', 'ashby')]
        if len(employers) == 1:
            for i in portals:
                groups[employers[0]].extend(groups[i])
                removed.add(i)
    result = []
    for index, group in enumerate(groups):
        if index in removed:
            continue
        group.sort(key=lambda j: (not bool(j.get('review', {}).get('applied_at')),
                                  j.get('review', {}).get('status', 'new') == 'new',
                                  j.get('availability') != 'active',
                                  j['source'].split(':')[0] not in ('greenhouse', 'lever', 'ashby'), j['key']))
        primary = dict(group[0])
        primary['duplicate_keys'] = [j['key'] for j in group[1:]]
        primary['source_links'] = list({(j['source'], j['url']): {'source': j['source'], 'url': j['url']} for j in group}.values())
        primary['freshness'] = freshness(primary, reference)
        primary['discovery_ready'] = (primary.get('availability') == 'active' and primary['freshness']['state'] == 'fresh'
                                      and primary.get('assessment', {}).get('entry_suitable', False))
        result.append(primary)
    return sorted(result, key=lambda j: (not j['discovery_ready'], -(timestamp(j.get('published_at')).timestamp() if timestamp(j.get('published_at')) else 0), -j.get('assessment', {}).get('score', 0)))
