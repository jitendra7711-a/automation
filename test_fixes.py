from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch
import test_app
from discovery import IST, applicants, decorate, freshness
from alerts import parse_alerts, ingest
from submissions import parse_receipts, reconcile


class TrackingAndFreshnessTests(unittest.TestCase):
    setUp = test_app.CareerOpsTests.setUp
    tearDown = test_app.CareerOpsTests.tearDown

    def qualified(self, count=24, hours=2):
        reference = datetime.now(IST)
        return dict(self.job, availability='active', assessment={'entry_suitable': True}, review={'status': 'new'},
                    published_at=(reference-timedelta(hours=hours)).isoformat(),
                    applicant_evidence={'count': count, 'label': str(count)+' applicants', 'observed_at': reference.isoformat()})

    def receipt(self, identifier='one', company='Example', title='DevOps Engineer'):
        return {'id': 'receipt-'+identifier, 'from': 'jobs-noreply@linkedin.com',
                'subject': 'Your application was sent to '+company, 'received_at': datetime.now(IST).isoformat(),
                'html': '<td><a href="https://www.linkedin.com/comm/jobs/view/'+identifier+'"><p>'+title+'</p><p>'+company+' · India (Remote)</p></a><p>Applied on November 17, 2026</p></td><h2>View similar jobs you may be interested in</h2><td><a href="https://www.linkedin.com/jobs/view/999"><p>DevOps Engineer</p><p>Recommendation · India</p></a></td>'}

    def test_24_hour_and_strict_25_applicant_boundaries(self):
        self.assertTrue(decorate(self.qualified())['pending'])
        self.assertFalse(decorate(self.qualified(25))['discovery_ready'])
        self.assertFalse(decorate(self.qualified(24, 24.01))['discovery_ready'])
        self.assertTrue(decorate(self.qualified(0))['discovery_ready'])
        missing = self.qualified(); missing.pop('applicant_evidence')
        self.assertFalse(decorate(missing)['discovery_ready'])
        self.assertIsNone(applicants(missing)['count'])

    def test_old_future_or_invalid_count_observations_do_not_qualify(self):
        job = self.qualified()
        for observed in [datetime.now(IST)-timedelta(hours=2), datetime.now(IST)+timedelta(hours=1)]:
            job['applicant_evidence']['observed_at'] = observed.isoformat()
            self.assertFalse(applicants(job)['eligible'])
        job['applicant_evidence'] = {'count': True, 'observed_at': datetime.now(IST).isoformat()}
        self.assertFalse(applicants(job)['eligible'])

    def test_upper_bound_badge_is_labelled_without_inventing_exact_count(self):
        job = self.qualified()
        job['applicant_evidence'] = {'upper_exclusive': 25, 'label': 'Be among the first 25 applicants', 'observed_at': datetime.now(IST).isoformat()}
        self.assertTrue(applicants(job)['eligible'])
        self.assertIsNone(applicants(job)['count'])
        job['applicant_evidence']['upper_exclusive'] = 50
        self.assertFalse(applicants(job)['eligible'])

    def test_alert_receipt_time_does_not_refresh_old_posting_or_count(self):
        received = datetime.now(IST)
        message = {'from': 'jobs-noreply@linkedin.com', 'received_at': received.isoformat(),
                   'html': '<td><a href="https://www.linkedin.com/jobs/view/123"><p>DevOps Engineer</p><p>Example · India</p></a><p>Posted 2 hours ago</p><p>18 applicants</p></td>'}
        job = parse_alerts([message])[0]
        self.assertEqual(applicants(job)['count'], 18)
        self.assertEqual(freshness(job)['state'], 'fresh')
        job = parse_alerts([dict(message, html=message['html'].replace('2 hours', '23 hours'))])[0]
        # Relative hour labels are rounded; oldest possible posting time is used.
        self.assertEqual(freshness(job, received+timedelta(minutes=1))['state'], 'stale')

    def test_submission_is_never_pending_even_if_it_has_a_cached_packet(self):
        job = self.qualified(); job.update(packet={'ready': True}, review={'status': 'applied', 'applied_at': datetime.now(IST).isoformat()})
        self.assertFalse(decorate(job)['pending'])
        self.assertFalse(decorate(job)['discovery_ready'])
        self.assertTrue(decorate(job)['submitted'])

    def test_repost_with_explicit_old_original_date_is_not_fresh(self):
        job = self.qualified(); job['description'] += ' Originally posted on: 29 Oct 2026'
        self.assertEqual(freshness(job)['state'], 'reposted_old')
        self.assertFalse(decorate(job)['discovery_ready'])

    def test_receipt_recommendations_are_not_marked_submitted(self):
        receipts = parse_receipts([self.receipt('123')])
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]['url'].endswith('/123'))
        self.assertNotIn('999', receipts[0]['url'])

    def test_real_confirmation_updates_existing_job_and_survives_reimport(self):
        receipt = self.receipt('123')
        job = dict(self.job, key='alerts:linkedin.com:123', source='alerts:linkedin.com',
                   url='https://www.linkedin.com/jobs/view/123', apply_url='https://www.linkedin.com/jobs/view/123')
        self.store.source_result(job['source'], jobs=[job])
        result = ingest(self.store, [receipt])
        self.assertEqual(result['submissions_updated'], 1)
        self.assertTrue(self.store.job(job['key'])['review']['applied_at'])
        result = ingest(self.store, [receipt])
        self.assertEqual(result['submissions_updated'], 0)
        self.store.source_result(job['source'], jobs=[job])
        self.assertTrue(self.store.job(job['key'])['review']['applied_at'])
        with self.store.db() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM events WHERE status='applied'").fetchone()['n']
        self.assertEqual(count, 1)

    def test_receipt_matches_all_exact_destination_copies(self):
        jobs = [dict(self.job, key='copy:'+str(i), source='copy:'+str(i), url='https://www.linkedin.com/jobs/view/123', apply_url='https://www.linkedin.com/jobs/view/123') for i in (1, 2)]
        for job in jobs: self.store.source_result(job['source'], jobs=[job])
        reconcile(self.store, parse_receipts([self.receipt('123')]))
        self.assertTrue(all(self.store.job(j['key'])['review']['applied_at'] for j in jobs))

    def test_confirmation_does_not_downgrade_an_interview(self):
        job = dict(self.job, url='https://www.linkedin.com/jobs/view/123', apply_url='https://www.linkedin.com/jobs/view/123')
        self.store.source_result(job['source'], jobs=[job])
        self.store.review(job['key'], 'interviewing')
        reconcile(self.store, parse_receipts([self.receipt('123')]))
        review = self.store.job(job['key'])['review']
        self.assertEqual(review['status'], 'interviewing')
        self.assertTrue(review['applied_at'])

    def test_unambiguous_naukri_title_match_and_recommendation_boundary(self):
        message = {'id': 'naukri-receipt', 'from': 'info@naukri.com', 'subject': 'You applied for 1 job on 16 Nov',
                   'received_at': datetime.now(IST).isoformat(),
                   'html': '<p>Applied on November 16, 2026</p><p>DevOps Engineer</p><p>Example</p><h2>Similar jobs for you</h2><p>Site Reliability Engineer</p><p>Other company</p>'}
        receipts = parse_receipts([message])
        self.assertEqual(len(receipts), 1)
        reconcile(self.store, receipts)
        self.assertTrue(self.store.job(self.job['key'])['review']['applied_at'])

    def test_no_title_only_match_when_two_requisitions_exist(self):
        second = dict(self.job, key='ashby:example:second', url='https://jobs.example.com/second', apply_url='https://jobs.example.com/second')
        self.store.source_result(self.job['source'], jobs=[self.job, second])
        receipt = {'id': 'ambiguous', 'portal': 'naukri.com', 'received_at': datetime.now(IST).isoformat(), 'url': '', 'title': self.job['title'], 'company': self.job['company']}
        self.assertEqual(reconcile(self.store, [receipt])['submissions_updated'], 0)

    def test_repeated_receipts_do_not_refetch_the_job_catalogue(self):
        receipt = parse_receipts([self.receipt('123')])[0]
        with patch.object(self.store, 'setting', return_value=[receipt]), patch.object(self.store, 'db', side_effect=AssertionError('Unexpected full catalogue read')):
            self.assertEqual(reconcile(self.store, [receipt])['submissions_updated'], 0)

    def test_deferred_receipt_is_retried_when_a_new_job_appears(self):
        receipt = {'id': 'later', 'portal': 'naukri.com', 'received_at': datetime.now(IST).isoformat(), 'url': '', 'title': 'SRE Engineer', 'company': 'Later Example'}
        self.assertEqual(reconcile(self.store, [receipt])['submissions_updated'], 0)
        later = dict(self.job, key='later', title='SRE Engineer', company='Later Example')
        self.store.source_result(later['source'], jobs=[later])
        self.assertEqual(reconcile(self.store, retry=True)['submissions_updated'], 1)

    def test_spoofed_senders_and_incomplete_confirmation_do_not_change_status(self):
        receipt = self.receipt('123')
        self.assertEqual(parse_receipts([dict(receipt, **{'from': 'jobs@linkedin.com.evil.test'})]), [])
        self.assertEqual(parse_receipts([dict(receipt, html='<p>No job details available</p>')]), [])


if __name__ == '__main__':
    unittest.main()
