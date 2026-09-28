import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';

test('renders an offline run and inspects its test evidence', async ({ page, baseURL }) => {
  const python = process.env.ANYBENCH_TEST_PYTHON || 'python';
  const fixture = spawnSync(python, ['tests/fixture.py'], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: '../src' },
  });
  expect(fixture.status, fixture.stderr).toBe(0);
  const id = fixture.stdout.trim();
  const token = readFileSync('.anybench/live/studio.token', 'utf8').trim();
  await page.goto(`${baseURL}/auth?credential=${token}&run=${id}`);
  await expect(page.getByRole('heading', { name: 'Pipeline whiteboard' })).toBeVisible();
  await expect(page.getByText('18 reasoning · 12 content')).toBeVisible();
  await expect(page.getByText('$0.9999 left of $1.00')).toBeVisible();
  await page.getByRole('button', { name: 'Open instructions' }).click();
  await expect(page.getByText('Fix the parser')).toBeVisible();
  await page.getByRole('button', { name: 'Close' }).click();
  await expect(page.locator('[data-id="builder"]')).toBeVisible();
  await expect(page.getByText('browser-001').first()).toBeVisible();
  await page.screenshot({ path: 'test-results/studio-whiteboard.png', fullPage: true });
  await page.locator('tbody tr').filter({ hasText: 'browser-001' }).click();
  await expect(page.getByRole('button', { name: 'Problem browser-001' })).toBeVisible();
  await page.getByRole('button', { name: 'anybench / local-candidate #1' }).click();
  await expect(page.getByText('Attempt #1').first()).toBeVisible();
  await expect(page.locator('[data-id="endpoint"]')).toBeVisible();
  await page.getByRole('button', { name: 'Tests', exact: true }).click();
  await expect(page.locator('.inspector .event-card').filter({ hasText: 'test.finished' })).toBeVisible();
  await page.screenshot({ path: 'test-results/studio-inspector.png', fullPage: true });
});

test('requires a local authenticated session for controls', async ({ request, baseURL }) => {
  const response = await request.post(`${baseURL}/api/experimental/v1/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/controls`,
    { data: { action: 'pause' }, headers: { Origin: 'https://example.invalid' } });
  expect(response.status()).toBe(403);
});

test('validates advanced workload before showing launch confirmation', async ({ page, baseURL }) => {
  const token = readFileSync('.anybench/live/studio.token', 'utf8').trim();
  await page.goto(`${baseURL}/auth?credential=${token}`);
  await page.getByRole('button', { name: '+ New benchmark' }).click();
  await page.getByText('Advanced CLI command').click();
  await page.locator('textarea').last().fill('["validate", "cases.csv"]');
  await page.getByRole('button', { name: 'Review workload' }).click();
  await expect(page.getByText('Validated workload')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Confirm and launch' })).toBeVisible();
});

test('lets a user set candidate harness, retries and budget from launch setup', async ({ page, baseURL }) => {
  const token = readFileSync('.anybench/live/studio.token', 'utf8').trim();
  let saved: any = null;
  await page.route('**/api/experimental/v1/config', async route => {
    if (route.request().method() === 'PUT') {
      saved = JSON.parse(route.request().postData() || '{}');
      await route.fulfill({ json: { saved: '.anybench/config.json' } });
    } else {
      await route.fulfill({ json: { repositories: [], models: [
        { name: 'builder', role: 'builder', model: 'local', base_url: 'http://localhost:1', api_key_env: 'BUILDER_KEY' },
        { name: 'candidate', role: 'candidate', model: 'local', base_url: 'http://localhost:1', api_key_env: 'CANDIDATE_KEY' },
      ] } });
    }
  });
  await page.goto(`${baseURL}/auth?credential=${token}`);
  await page.getByRole('button', { name: '+ New benchmark' }).click();
  await page.getByRole('button', { name: 'Choose harness, retry and budget' }).click();
  await page.getByLabel('HTTP retries').fill('2');
  await page.getByLabel('Run budget, USD').fill('5');
  await page.getByLabel('Harness').selectOption('codex');
  await page.getByLabel('Docker image').fill('codex-image:latest');
  await page.getByLabel('Allowed hosts, comma separated').fill('api.example.com');
  await page.getByRole('button', { name: 'Save configuration' }).click();
  expect(saved.models[1]).toMatchObject({ harness: 'codex', max_retries: 2,
    image: 'codex-image:latest', allowed_hosts: ['api.example.com'] });
  expect(saved.budget_usd).toBe(5);
  await expect(page.getByRole('heading', { name: 'Launch a benchmark' })).toBeVisible();
});
