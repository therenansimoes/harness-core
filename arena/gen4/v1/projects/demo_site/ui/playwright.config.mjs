import { defineConfig } from '@playwright/test';

// Porta por env para que rodadas simultâneas (ex.: testes sobre cópias do
// projeto) não briguem pela mesma porta nem sirvam o site errado.
const PORT = process.env.HARNESS_UI_PORT || 4173;

// Serve o site estático e roda os checks no Chrome do sistema (channel: 'chrome'),
// para não precisar baixar bundle de browser.
export default defineConfig({
  testDir: './tests',
  // Baselines versionadas ficam em ui/baselines/, não espalhadas por *-snapshots/.
  snapshotPathTemplate: '{testDir}/../baselines/{arg}{ext}',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  reporter: [['json', { outputFile: 'report.json' }], ['list']],
  webServer: {
    command: `python3 -m http.server ${PORT} --directory ../site`,
    url: `http://127.0.0.1:${PORT}/index.html`,
    // false de propósito: com reuse, um servidor sobrando de outra rodada
    // serviria o site de OUTRO projeto na mesma porta, e a suite passaria
    // verificando o arquivo errado. Porta por env resolve o conflito sem mentir.
    reuseExistingServer: false,
    timeout: 30000,
  },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: 'chrome',
    viewport: { width: 1280, height: 800 },
  },
  // Threshold: pixel-perfect é frágil (antialiasing, fonte, versão do Chrome).
  // 2% pega quebra real de layout sem alarme falso a cada patch do browser.
  expect: { toHaveScreenshot: { maxDiffPixelRatio: 0.02, animations: 'disabled' } },
});
