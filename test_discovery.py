from datetime import datetime, timedelta
import json
import re
import unittest
from unittest.mock import patch

import test_app
from discovery import IST, canonical_url, freshness, unique_jobs
from alerts import parse_alerts
from core import assess, normalize, run_scan


class DiscoveryTests(unittest.TestCase):
    setUp = test_app.CareerOpsTests.setUp
    tearDown = test_app.CareerOpsTests.tearDown
    def test_rolling_72_hours_and_unknown_dates(self):
        reference = datetime(2026, 11, 16, 1, 0, tzinfo=IST)
        for hours, expected in [(0, 'fresh'), (71.99, 'fresh'), (72, 'fresh'), (72.01, 'stale'), (-1, 'future')]:
            self.assertEqual(freshness({'published_at': (reference-timedelta(hours=hours)).isoformat()}, reference)['state'], expected)
        for job in ({'first_seen': reference.isoformat()}, {'published_at': reference.replace(tzinfo=None).isoformat()}, {'published_at': reference.isoformat(), 'date_kind': 'created'}):
            self.assertEqual(freshness(job, reference)['state'], 'undated')
        self.assertEqual(freshness({'published_at': '2026-11-15T20:00:00Z'}, reference)['day'], '2026-11-16')

    def test_url_dedup_preserves_requisition_parameters(self):
        self.assertEqual(canonical_url('https://example.com/job?utm_source=x&req=123&gh_src=z'), 'https://example.com/job?req=123')
        self.assertNotEqual(canonical_url('https://example.com/job?req=123'), canonical_url('https://example.com/job?req=456'))

    def test_duplicate_portals_keep_one_record_and_submission_history(self):
        first = dict(self.job, availability='active', review={'status': 'new'}, assessment={'entry_suitable': True})
        second = dict(first, key='portal:one', source='jobicy:devops', review={'status': 'applied', 'applied_at': '2026-11-16T12:00:00+05:30'}, apply_url=first['url']+'?utm_source=portal')
        jobs = unique_jobs([first, second])
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]['review']['applied_at'])
        self.assertEqual(len(jobs[0]['source_links']), 2)

    def test_identical_titles_with_different_employer_requisitions_stay_distinct(self):
        first = dict(self.job, availability='active')
        second = dict(first, key='ashby:example:two', url=first['url']+'/two', apply_url=first['url']+'/two')
        portal = dict(first, key='portal:one', source='jobicy:devops', url='https://jobicy.com/jobs/123', apply_url='https://jobicy.com/jobs/123')
        self.assertEqual(len(unique_jobs([first, second, portal])), 3)

    def test_entry_experience_filter_rejects_senior_and_three_year_minimum(self):
        profile = self.store.setting('profile')
        for title, description, expected in [('DevOps Engineer', '0-2 years of experience', True), ('Site Reliability Engineer', '2+ years of experience', True), ('DevOps Engineer', '3-5 years of experience', False), ('Senior SRE', '2 years of experience', False), ('SRE', 'AWS and Linux', False), ('Platform Engineer', '2 years of experience', False)]:
            self.assertEqual(assess(dict(self.job, title=title, description=description), profile)['entry_suitable'], expected)

    def test_scan_prepares_fresh_entry_only_and_retains_old_submitted(self):
        reference = datetime.now(IST)
        fresh = dict(self.raw, id='fresh', jobUrl='https://jobs.ashbyhq.com/example/fresh', publishedAt=reference.isoformat())
        old = dict(self.raw, id='old', jobUrl='https://jobs.ashbyhq.com/example/old', publishedAt=(reference-timedelta(days=4)).isoformat())
        self.store.save_document('resume', b'%PDF-test', 'application/pdf')
        self.store.review(self.job['key'], 'applied', confirmed=True)
        (self.root/'companies.json').write_text(json.dumps([self.company]))
        token = self.store.acquire()
        with self.store.db() as db:
            db.execute("INSERT INTO runs(id,started_at,status) VALUES ('test',?,'running')", (reference.isoformat(),))
        with patch('core.fetch', return_value={'jobs': [fresh, old]}):
            run_scan(self.store, 'test', token)
        by_key = {j['key']: j for j in self.store.jobs()}
        self.assertTrue(by_key['ashby:example:fresh']['packet'])
        self.assertIsNone(by_key['ashby:example:old']['packet'])
        self.assertTrue(by_key[self.job['key']]['review']['applied_at'])

    def test_alert_dates_are_not_falsely_treated_as_posting_dates(self):
        received = datetime.now(IST).isoformat()
        message = {'from': 'LinkedIn <jobs-noreply@linkedin.com>', 'received_at': received,
                   'html': '<table><tr><td><a href="https://www.linkedin.com/comm/jobs/view/123?midToken=secret"><p>DevOps Engineer</p><p>Example · India (Remote)</p></a></td></tr></table>'}
        jobs = parse_alerts([message, message])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['company'], 'Example')
        self.assertEqual(jobs[0]['published_at'], '')
        self.assertEqual(freshness(jobs[0])['state'], 'undated')
        self.assertNotIn('secret', json.dumps(jobs))
        self.assertEqual(parse_alerts([dict(message, **{'from': 'x@linkedin.com.evil.test'})]), [])
        self.assertEqual(parse_alerts([dict(message, subject='You applied for 15 jobs')]), [])

    def test_naukri_alert_link_is_imported_without_requesting_portal(self):
        message = {'from': 'alerts@naukri.com', 'received_at': datetime.now(IST).isoformat(),
                   'html': '<td><a href="https://www.naukri.com/job-listings-devops-example-123?src=email"><p>DevOps Engineer</p></a><p>Example · Bengaluru</p><p>1-2 years of experience</p></td>'}
        jobs = parse_alerts([message])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['source'], 'alerts:naukri.com')

    def test_private_bridge_requires_auth_and_dedicated_bearer(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/api/alerts/script').status_code, 401)
        self.assertEqual(anonymous.post('/api/alerts/ingest', json={'messages': []}).status_code, 401)
        response = self.client.get('/api/alerts/script')
        token = re.search(r'const CAREEROPS_TOKEN = "([a-f0-9]+)"', response.get_data(as_text=True)).group(1)
        result = anonymous.post('/api/alerts/ingest', json={'messages': []}, headers={'Authorization': 'Bearer '+token})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.store.setting('alert_bridge')['bridge_connected_at'])
        result = anonymous.post('/api/alerts/ingest', json={'messages': [None]}, headers={'Authorization': 'Bearer '+token})
        self.assertEqual(result.status_code, 400)


if __name__ == '__main__':
    import unittest
    unittest.main()
