import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ['CAREEROPS_FACTORY_ONLY'] = '1'
from app import create_app
from core import ROOT, normalize, assess, experience_evidence, now


class CareerOpsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for name in ('profile.json', 'companies.json'):
            shutil.copyfile(ROOT / name, self.root / name)
        self.app = create_app(self.root, testing=True, start_scheduler=False, database_url='')
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        profile = self.store.setting('profile')
        profile.update(display_name='Test Candidate', resume_facts=['Supported AKS upgrades across 7+ environments.'], evidence_for_outreach='Supported AKS upgrades across 7+ environments.')
        self.store.save_setting('profile', profile)
        self.company = {'name': 'Example', 'provider': 'ashby', 'board': 'example'}
        self.raw = {'id': 'one', 'title': 'DevOps Engineer', 'jobUrl': 'https://jobs.ashbyhq.com/example/one', 'location': 'Bengaluru', 'workplaceType': 'Hybrid', 'isRemote': True, 'isListed': True, 'descriptionPlain': '2-4 years of experience. AWS, Docker, Kubernetes, Terraform, Python, CI/CD.'}
        self.job = normalize(self.company, {'jobs': [self.raw]})[0]
        self.store.source_result('ashby:example', jobs=[self.job])
        with self.client.session_transaction() as session:
            session['authenticated'] = True
            session['csrf'] = 'test-csrf'
        self.headers = {'X-CSRF-Token': 'test-csrf'}

    def tearDown(self):
        self.directory.cleanup()

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def test_private_routes_require_authentication(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/api/bootstrap').status_code, 401)
        self.assertEqual(anonymous.get('/api/resume').status_code, 401)
        self.assertEqual(anonymous.get('/api/export').status_code, 401)
        self.assertEqual(anonymous.get('/').status_code, 302)

    def test_health_is_public_and_does_not_scan_or_disclose_resume(self):
        anonymous = self.app.test_client()
        with patch('app.run_scan') as scanner:
            response = anonymous.get('/healthz')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {'status': 'ok', 'service': 'careerops'})
            scanner.assert_not_called()
        self.assertNotIn('Jitendra', response.get_data(as_text=True))

    def test_csrf_and_origin_validation(self):
        body = {'key': self.job['key'], 'status': 'shortlisted'}
        self.assertEqual(self.client.post('/api/jobs/review', json=body).status_code, 403)
        headers = dict(self.headers, Origin='https://untrusted.example')
        self.assertEqual(self.client.post('/api/jobs/review', json=body, headers=headers).status_code, 403)
        self.assertEqual(self.post('/api/jobs/review', body).status_code, 200)

    def test_applied_requires_actual_submission_confirmation(self):
        body = {'key': self.job['key'], 'status': 'applied'}
        self.assertEqual(self.post('/api/jobs/review', body).status_code, 400)
        self.assertFalse(self.store.job(self.job['key'])['review'].get('applied_at'))
        response = self.post('/api/jobs/review', dict(body, confirmed=True))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['review']['applied_at'])
        with self.store.db() as db:
            self.assertEqual(len(db.execute('SELECT * FROM events').fetchall()), 1)

    def test_prepared_is_not_applied(self):
        self.store.save_document('resume', b'%PDF-test-resume', 'application/pdf')
        result = self.post('/api/jobs/packet', {'key': self.job['key']})
        self.assertEqual(result.status_code, 200)
        job = self.store.job(self.job['key'])
        self.assertEqual(job['review']['status'], 'prepared')
        self.assertFalse(job['review'].get('applied_at'))
        self.assertNotIn('I have applied', result.json['outreach'])
        self.assertIn('7+ environments', result.json['cover_letter'])

    def test_packet_requires_resume(self):
        result = self.post('/api/jobs/packet', {'key': self.job['key']})
        self.assertEqual(result.status_code, 400)

    def test_profile_changes_invalidate_readiness_without_erasing_application_history(self):
        self.store.save_document('resume', b'%PDF-test-resume', 'application/pdf')
        self.store.packet(self.job['key'])
        self.assertEqual(self.store.job(self.job['key'])['review']['status'], 'prepared')
        self.assertEqual(self.post('/api/profile', {'evidence_for_outreach': 'Updated verified achievement'}).status_code, 200)
        job = self.store.job(self.job['key'])
        self.assertIsNone(job['packet'])
        self.assertEqual(job['review']['status'], 'shortlisted')

    def test_regenerating_packet_after_application_uses_recorded_truth(self):
        self.store.save_document('resume', b'%PDF-test-resume', 'application/pdf')
        self.store.packet(self.job['key'])
        self.store.review(self.job['key'], 'applied', confirmed=True)
        self.assertIn('I have applied', self.store.packet(self.job['key'])['outreach'])

    def test_source_failures_preserve_previous_snapshot_and_manual_status(self):
        self.store.review(self.job['key'], 'applied', confirmed=True)
        self.store.source_result('ashby:example', error='timeout')
        job = self.store.job(self.job['key'])
        self.assertEqual(job['availability'], 'active')
        self.assertTrue(job['review']['applied_at'])
        self.store.source_result('ashby:example', jobs=[self.job])
        with self.store.db() as db:
            self.assertEqual(len(db.execute('SELECT * FROM jobs').fetchall()), 1)

    def test_missing_job_is_absent_not_filled(self):
        self.store.source_result('ashby:example', jobs=[])
        self.assertEqual(self.store.job(self.job['key'])['availability'], 'not_in_latest_feed')

    def test_lease_blocks_overlap_and_releases(self):
        token = self.store.acquire()
        self.assertTrue(token)
        self.assertIsNone(self.store.acquire())
        self.store.release('wrong-token')
        self.assertIsNone(self.store.acquire())
        self.store.release(token)
        self.assertTrue(self.store.acquire())

    def test_no_auto_submission_mode(self):
        self.assertEqual(self.post('/api/settings', {'submission_mode': 'submit'}).status_code, 400)
        self.assertEqual(self.client.post('/api/apply', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post('/api/automation/run').status_code, 401)

    def test_hourly_schedule_is_default_and_can_be_saved(self):
        self.assertEqual(self.store.setting('automation')['interval_hours'], 1)
        response = self.post('/api/settings', {'enabled': True, 'interval_hours': 1, 'submission_mode': 'prepare'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['automation']['interval_hours'], 1)

    def test_country_and_hybrid_are_not_inferred_from_company_description(self):
        self.assertEqual(self.job['workplace'], 'hybrid')
        us = dict(self.job, location='Remote (US)', description='AWS. We also have offices in India.', workplace='remote')
        self.assertFalse(assess(us, self.store.setting('profile'))['relevant'])
        self.assertEqual(normalize(self.company, {'jobs': [dict(self.raw, isListed=False)]}), [])

    def test_live_experience_wordings_are_flagged(self):
        examples = [('3+ years in DevOps, SRE, or platform infrastructure engineering', 3), ('4-5 years of hands-on systems experience', 4), ('4\u20137+ years of DevOps / Cloud / Infrastructure experience', 4), ('5+ years building infrastructure or platform software', 5)]
        for text, minimum in examples:
            self.assertEqual(experience_evidence(text)[0][0], minimum)
        self.assertEqual(experience_evidence('Our company is 10 years old.'), [])

    def test_invalid_feed_does_not_replace_data(self):
        with self.assertRaises(ValueError):
            normalize(self.company, {'error': 'unavailable'})
        with self.assertRaises(ValueError):
            normalize(self.company, {'jobs': [dict(self.raw, jobUrl='javascript:alert(1)')]})
        with self.assertRaises(ValueError):
            normalize(self.company, {'jobs': [self.raw, self.raw]})

    def test_invalid_resume_upload_rejected(self):
        response = self.client.post('/api/resume', data={'file': (io.BytesIO(b'not a PDF'), 'resume.pdf')}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_export_has_history_but_no_session_secret(self):
        response = self.client.get('/api/export')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('csrf_token', response.json)
        self.assertIn('events', response.json)

    def test_render_rejects_ephemeral_storage_and_preview_auth(self):
        with patch.dict(os.environ, {'RENDER': 'true', 'DATABASE_URL': ''}):
            with self.assertRaises(RuntimeError):
                create_app(self.root, testing=True, database_url='', start_scheduler=False)
            with self.assertRaises(RuntimeError):
                create_app(self.root, testing=True, preview=True, start_scheduler=False)


if __name__ == '__main__':
    unittest.main()
