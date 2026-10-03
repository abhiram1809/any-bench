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
  await expect(page.locator('.react-flow__edge')).toHaveCount(8);
  await expect(page.getByText('browser-001').first()).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/studio-whiteboard.png', fullPage: true });
  await page.locator('tbody tr').filter({ hasText: 'browser-001' }).click();
  await expect(page.getByRole('button', { name: 'Problem browser-001' })).toBeVisible();
  await page.getByRole('button', { name: 'anybench / local-candidate #1' }).click();
  await expect(page.getByText('Attempt #1').first()).toBeVisible();
  await expect(page.locator('[data-id="endpoint"]')).toBeVisible();
  await page.getByRole('tab', { name: 'Tests', exact: true }).click();
  await expect(page.locator('.inspector .event-card').filter({ hasText: 'test.finished' })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
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
  await page.getByRole('button', { name: 'New benchmark' }).click();
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
  await page.getByRole('button', { name: 'New benchmark' }).click();
  await page.getByRole('button', { name: 'Choose harness, retry and budget' }).click();
  await page.getByLabel('HTTP retries').fill('2');
  await page.getByLabel('Run budget, USD').fill('5');
  await page.getByRole('combobox', { name: 'Harness', exact: true }).selectOption('codex');
  await page.getByLabel('Docker image').fill('codex-image:latest');
  await page.getByLabel('Allowed hosts, comma separated').fill('api.example.com');
  await page.getByRole('button', { name: 'Save configuration' }).click();
  expect(saved.models[1]).toMatchObject({ harness: 'codex', max_retries: 2,
    image: 'codex-image:latest', allowed_hosts: ['api.example.com'] });
  expect(saved.budget_usd).toBe(5);
  await expect(page.getByRole('heading', { name: 'Launch a benchmark' })).toBeVisible();
});

test('dialogs contain focus, close on Escape, and restore their trigger', async ({ page, baseURL }) => {
  const token = readFileSync('.anybench/live/studio.token', 'utf8').trim();
  await page.goto(`${baseURL}/auth?credential=${token}`);
  const trigger = page.getByRole('button', { name: 'New benchmark' });
  await trigger.click();
  const dialog = page.getByRole('dialog', { name: 'Launch a benchmark' });
  await expect(dialog).toBeVisible();
  for (let index = 0; index < 18; index++) {
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test('mobile run keeps navigation, logos and keyboard evidence tabs usable', async ({ page, baseURL }) => {
  const fixture = spawnSync(process.env.ANYBENCH_TEST_PYTHON || 'python', ['tests/fixture.py'], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: '../src' },
  });
  expect(fixture.status, fixture.stderr).toBe(0);
  const token = readFileSync('.anybench/live/studio.token', 'utf8').trim();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${baseURL}/auth?credential=${token}&run=${fixture.stdout.trim()}`);
  await expect(page.getByRole('button', { name: 'Attach saved run' })).toBeVisible();
  await expect(page.locator('.comparison .brand-row')).toContainText('OpenRouter');
  await expect(page.locator('.comparison .brand-row img')).toHaveCount(2);
  const overview = page.getByRole('tab', { name: 'Overview', exact: true });
  await overview.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'Prompt/Response' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/studio-mobile.png', fullPage: true });
});

test('offline report embeds provider logos, sorts, filters and fits mobile', async ({ page, baseURL }) => {
  const fixture = spawnSync(process.env.ANYBENCH_TEST_PYTHON || 'python', ['tests/fixture.py'], {
    encoding: 'utf8', env: { ...process.env, PYTHONPATH: '../src' },
  });
  expect(fixture.status, fixture.stderr).toBe(0);
  const requests: string[] = [];
  page.on('request', request => requests.push(request.url()));
  await page.route('**/report-preview', route => route.fulfill({
    contentType: 'text/html', body: readFileSync('.anybench/browser-fixture/report.html', 'utf8'),
  }));
  await page.goto(`${baseURL}/report-preview`);
  for (const name of ['OpenAI', 'Anthropic', 'OpenRouter', 'Custom endpoint']) {
    await expect(page.locator('.candidate-grid .brand-chip').filter({ hasText: `Provider: ${name}` })).toBeVisible();
  }
  expect(await page.locator('.brand-chip img').evaluateAll(images => images.every(image => (image as HTMLImageElement).naturalWidth > 0))).toBe(true);
  await page.locator('#models').getByRole('button', { name: 'Test success', exact: true }).click();
  await expect(page.locator('#models tbody tr').first()).toContainText('OpenCode candidate');
  await page.getByLabel('Search', { exact: true }).fill('parser-000');
  await expect(page.locator('#attempts tbody tr:visible')).toHaveCount(4);
  await page.getByLabel('Search', { exact: true }).fill('no matching case');
  await expect(page.locator('#result-count')).toHaveText('0 of 20 attempts');
  await page.getByLabel('Search', { exact: true }).clear();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/report-desktop.png', fullPage: true });
  await page.screenshot({ path: 'test-results/report-overview.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: 'test-results/report-mobile.png', fullPage: true });
  expect(requests).toEqual([`${baseURL}/report-preview`]);
});
