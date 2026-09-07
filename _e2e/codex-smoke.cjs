const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1280, height: 720}, recordVideo: {dir: path.join(__dirname, 'videos')}});
  const page = await context.newPage();
  await page.setContent('<body style="background:#101b3e;color:white;font:18px monospace"><h1>OCMO / real Codex CLI</h1><button id="run">Run live smoke test</button><pre style="white-space:pre-wrap"></pre></body>');
  let result;
  await page.exposeFunction('runSmoke', () => new Promise(resolve => {
    const env = {...process.env, PYTHONUNBUFFERED: '1'};
    if (env.OCMO_SMOKE_INSTALLED === '1') delete env.PYTHONPATH;
    else env.PYTHONPATH = path.join(path.dirname(__dirname), 'src');
    const child = spawn('python', [path.join(__dirname, 'codex-smoke.py')], {
      cwd: path.dirname(__dirname), env
    });
    let output = '';
    let updates = Promise.resolve();
    for (const stream of [child.stdout, child.stderr]) stream.on('data', chunk => {
      const text = chunk.toString(); process.stdout.write(text); output += text;
      updates = updates.then(() => page.locator('pre').evaluate((el, text) => { el.textContent = text; window.scrollTo(0, document.body.scrollHeight); }, output));
    });
    child.on('close', code => { updates.then(() => { result = code; resolve(code); }); });
  }));
  await page.evaluate(() => document.querySelector('button').onclick = async () => { const code = await window.runSmoke(); document.title = 'FINISHED ' + code; });
  await page.click('button');
  await page.waitForFunction(() => document.title.startsWith('FINISHED'), null, {timeout: 400000});
  await page.waitForTimeout(1500);
  const video = page.video(); await context.close(); await browser.close();
  console.log('Recording:', await video.path()); process.exitCode = result;
})().catch(error => { console.error(error); process.exitCode = 1; });
