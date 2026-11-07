import { readFileSync, existsSync } from 'node:fs';

const files = ['src/lib/gpu-api.ts', 'src/components/GpuParamForm.tsx', 'src/App.tsx', 'src/styles.css'];
for (const file of files) if (!existsSync(file)) throw new Error(`missing standalone GPU file: ${file}`);
const api = readFileSync('src/lib/gpu-api.ts', 'utf8');
for (const symbol of ['unwrapGpuResponse', 'createGpuJob', 'getGpuWorkflowSchema', 'Idempotency-Key', 'login', 'restoreSession', '/api/auth/session']) {
  if (!api.includes(symbol)) throw new Error(`missing GPU contract symbol: ${symbol}`);
}
const app = readFileSync('src/App.tsx', 'utf8');
for (const marker of ['创作', '任务', '作品', '工作流', '设置', 'role="dialog"']) {
  if (!app.includes(marker)) throw new Error(`missing business UI marker: ${marker}`);
}
console.log('standalone GPU contract markers present');
