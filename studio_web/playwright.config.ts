import { defineConfig } from '@playwright/test';

const python = process.env.ANYBENCH_TEST_PYTHON || 'python';
export default defineConfig({
  testDir: './tests',
  use: { baseURL: 'http://127.0.0.1:8766', viewport: { width: 1440, height: 900 } },
  webServer: {
    command: `${python} -m anybench.cli studio --no-open --port 8766`,
    url: 'http://127.0.0.1:8766/health',
    env: { PYTHONPATH: '../src' },
    reuseExistingServer: false,
    timeout: 30000,
  },
  reporter: 'list',
});
