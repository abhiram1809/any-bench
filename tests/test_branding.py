import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from anybench.branding import catalog, configured_host, endpoint_host, provider_brand
from anybench.metrics import summary
from anybench.model import Case, ModelConfig, RunRecord, read_jsonl, write_jsonl
from anybench.report import report
from anybench.runner import run_one


class BrandingTests(unittest.TestCase):
    def test_routes_by_endpoint_not_model_name_or_protocol(self):
        record = RunRecord('c', 'OpenAI candidate', 1, 'completed', 2,
                           harness='codex', model_id='openai/model',
                           provider_host='openrouter.ai')
        brands = summary([record])['groups'][0]['branding']
        self.assertEqual(brands['harness']['label'], 'Codex')
        self.assertEqual(brands['providers'], [{'label': 'OpenRouter', 'icon': 'openrouter'}])
        for host, label in [('api.openai.com', 'OpenAI'), ('API.ANTHROPIC.COM.', 'Anthropic'),
                            ('api.deepseek.com', 'DeepSeek'), ('api.groq.com', 'Groq')]:
            with self.subTest(host=host):
                self.assertEqual(provider_brand(host)['label'], label)

    def test_unknown_and_local_endpoints_do_not_impersonate_providers(self):
        for host in ['api.openai.com.attacker.test', 'openrouter.ai.attacker.test', 'private.example']:
            self.assertEqual(provider_brand(host), {'label': 'Custom endpoint', 'icon': 'generic'})
        for host in ['localhost', '127.0.0.1', '::1']:
            self.assertEqual(provider_brand(host)['label'], 'Local endpoint')
        self.assertEqual(provider_brand('')['label'], 'Provider unavailable')

    def test_hostname_storage_excludes_url_secrets(self):
        self.assertEqual(endpoint_host('https://user:secret@api.openai.com/private?key=secret#secret'),
                         'api.openai.com')
        for url in ['file:///etc/passwd', 'https://[broken', 'https://<script>', 'https://api.openai.com:bad']:
            self.assertEqual(endpoint_host(url), '')

    def test_default_and_environment_endpoints(self):
        self.assertEqual(configured_host({'harness': 'codex'}), 'api.openai.com')
        self.assertEqual(configured_host({'harness': 'claude'}), 'api.anthropic.com')
        self.assertEqual(configured_host({'harness': 'opencode'}), '')
        config = {'harness': 'codex', 'env': {'OPENAI_BASE_URL': 'TEST_ENDPOINT'}}
        with patch.dict('os.environ', {'TEST_ENDPOINT': 'https://openrouter.ai/api/v1'}):
            self.assertEqual(configured_host(config, environment=True), 'openrouter.ai')
            self.assertEqual(configured_host(config), '')
            config['base_url'] = 'https://api.openai.com/v1'
            self.assertEqual(configured_host(config, environment=True), 'api.openai.com')

    def test_runner_keeps_provider_identity_on_failed_attempt(self):
        case = Case('c', 'repo', 'base', 'gold', 'Fix', '', '')
        config = ModelConfig('candidate', 'https://openrouter.ai/api/v1', 'openai/model', 'KEY')
        with patch('anybench.runner.Sandbox', side_effect=RuntimeError('fixture failure')):
            record = run_one(case, config)
        self.assertEqual(record.status, 'error')
        self.assertEqual(record.provider_host, 'openrouter.ai')

    def test_old_records_and_manifest_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.jsonl'
            path.write_text(json.dumps({'case_id': 'c', 'model': 'm', 'attempt': 1,
                                        'status': 'completed', 'seconds': 1}) + '\n')
            records = read_jsonl(path)
            self.assertIsNone(records[0].provider_host)
            manifest = {'inputs': {'run_inputs': {'configs': [
                {'name': 'm', 'base_url': 'https://api.anthropic.com/v1'}]}}}
            brands = summary(records, manifest=manifest)['groups'][0]['branding']
            self.assertEqual(brands['providers'][0]['label'], 'Anthropic')
            records[0].provider_host = 'openrouter.ai'
            write_jsonl(path, records)
            brands = summary(read_jsonl(path), manifest=manifest)['groups'][0]['branding']
            self.assertEqual(brands['providers'][0]['label'], 'OpenRouter')
        brands = summary([RunRecord('c', 'gpt-model', 1, 'completed', 1)])['groups'][0]['branding']
        self.assertEqual(brands['providers'][0]['label'], 'Provider unavailable')

    def test_mixed_endpoint_group_does_not_choose_one_provider(self):
        records = [RunRecord('a', 'm', 1, 'completed', 1, provider_host='api.openai.com'),
                   RunRecord('b', 'm', 1, 'completed', 1, provider_host='openrouter.ai')]
        brands = summary(records)['groups'][0]['branding']
        self.assertEqual([brand['label'] for brand in brands['providers']], ['OpenAI', 'OpenRouter'])

    def test_report_embeds_logos_and_preserves_private_export_boundary(self):
        records = [RunRecord('PRIVATE_CASE', '<script>candidate</script>', 1, 'completed', 1,
                             harness='claude', provider_host='api.anthropic.com',
                             diff='PRIVATE_PATCH')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.html'
            report(records, path, aggregate_only=True)
            document = path.read_text()
        self.assertIn('Harness: Claude Code', document)
        self.assertIn('Provider: Anthropic', document)
        self.assertIn('data:image/svg+xml;base64,', document)
        self.assertNotIn('<img src="http', document)
        self.assertNotIn('PRIVATE_CASE', document)
        self.assertNotIn('PRIVATE_PATCH', document)
        self.assertNotIn('<script>candidate</script>', document)
        self.assertIn('&lt;script&gt;candidate&lt;/script&gt;', document)

    def test_embedded_svg_assets_have_no_external_references_or_scripts(self):
        for name, uri in catalog()['icons'].items():
            with self.subTest(icon=name):
                self.assertTrue(uri.startswith('data:image/svg+xml;base64,'))
                root = ET.fromstring(base64.b64decode(uri.split(',')[1]))
                for element in root.iter():
                    self.assertNotIn(element.tag.split('}')[-1], ['script', 'image', 'foreignObject'])
                    self.assertFalse(any(key.lower().startswith('on') or key.endswith('href')
                                         for key in element.attrib))
