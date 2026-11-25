import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright';

const temp = await mkdtemp(join(tmpdir(), 'fanntv-call-'));
const env = {...process.env, FANNTV_DB: join(temp, 'test.db'), FANNTV_ENV: 'development', FANNTV_STUN_URL: ''};
execFileSync('python3', ['-c', `import server
server.initialize()
with server.connect() as db:
    db.execute("UPDATE channels SET stream='/media/demo.mp4' WHERE id=2")
`], {env});
const child = spawn('python3', ['-u', '-c', `import server
http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
print(f'http://127.0.0.1:{http.server_port}')
http.serve_forever()
`], {env});
let browser;
try {
  const base = await new Promise((resolve, reject) => {
    child.stdout.once('data', data => {
      const url = data.toString().match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
      if (url) resolve(url);
      else reject(new Error(data.toString()));
    });
    child.once('error', reject);
    child.once('exit', code => reject(new Error('Test server exited: ' + code)));
  });
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
    args: ['--no-sandbox', '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream']
  });
  async function register(name) {
    const page = await browser.newPage({locale: 'en-US'});
    // Run the browser workflow in environments that restrict real ICE networking.
    if (process.env.FANNTV_TEST_MOCK_WEBRTC === '1') {
      await page.addInitScript(() => {
        window.RTCPeerConnection = class extends EventTarget {
          iceGatheringState = 'complete';
          connectionState = 'new';
          async createOffer() { return {type: 'offer', sdp: 'v=0\r\ns=offer\r\n'}; }
          async createAnswer() { return {type: 'answer', sdp: 'v=0\r\ns=answer\r\n'}; }
          addTrack(track, stream) { this.stream = stream; }
          async setLocalDescription(description) {
            this.localDescription = description;
            this.connect();
          }
          async setRemoteDescription(description) {
            this.remoteDescription = description;
            this.connect();
          }
          connect() {
            if (!this.localDescription || !this.remoteDescription) return;
            this.connectionState = 'connected';
            const event = new Event('track');
            event.streams = [this.stream];
            this.dispatchEvent(event);
            this.dispatchEvent(new Event('connectionstatechange'));
          }
          close() { this.connectionState = 'closed'; }
        };
      });
    }
    await page.goto(base);
    const reply = await page.request.post(base + '/api/register', {
      data: {name, email: name + '@example.com', password: 'browser-test-password'}
    });
    assert.equal(reply.status(), 200);
    await page.reload();
    await page.getByRole('heading', {name: new RegExp('Good .*' + name)}).waitFor();
    return page;
  }
  const host = await register('Host');
  const guest = await register('Guest');
  await host.getByRole('button', {name: 'Watch NATURE+', exact: true}).click();
  await host.getByRole('dialog').getByRole('button', {name: 'Video call', exact: true}).click();
  const link = host.getByRole('textbox', {name: 'Video call invite link'});
  await link.waitFor();
  await guest.goto(await link.inputValue());
  await guest.getByRole('button', {name: 'Accept video call'}).click();
  for (const page of [host, guest]) {
    await page.getByText('Video call connected.', {exact: true}).waitFor({timeout: 45000});
    await page.waitForFunction(() => document.querySelector('.call-remote')?.srcObject?.getTracks().length === 2);
  }
  await host.getByRole('button', {name: 'Mute microphone'}).click();
  await host.getByRole('button', {name: 'Turn camera off'}).click();
  assert(await host.locator('.call-local').evaluate(video => video.srcObject.getTracks().every(track => !track.enabled)));

  await guest.getByRole('button', {name: 'Watch NATURE+', exact: true}).click();
  await guest.getByRole('dialog').getByRole('heading', {name: 'NATURE+'}).waitFor();
  await host.getByRole('button', {name: 'Close', exact: true}).click();
  await host.getByRole('button', {name: 'Watch WORLD NEWS', exact: true}).click();
  await host.getByRole('dialog').getByRole('heading', {name: 'WORLD NEWS'}).waitFor();
  assert(await host.getByText('Video call connected.', {exact: true}).isVisible());
  await host.locator('#player').evaluate(async video => { video.loop = true; await video.play(); });
  assert(await guest.getByText('Video call connected.', {exact: true}).isVisible());
  assert(await guest.getByRole('dialog').getByRole('heading', {name: 'NATURE+'}).isVisible());
  await host.getByRole('button', {name: 'End call', exact: true}).click();
  await guest.getByText('This video call has ended or the invitation expired.', {exact: true}).waitFor();
  assert.equal(await host.locator('#player').evaluate(video => video.paused), false);
  assert.equal(await host.locator('.call-local').count(), 0);
  console.log('Video call connected, survived channel changes, and ended independently of TV playback.');
} finally {
  await browser?.close();
  if (child.exitCode === null && child.signalCode === null) {
    await new Promise(resolve => { child.once('exit', resolve); child.kill(); });
  }
  await rm(temp, {recursive: true, force: true});
}
