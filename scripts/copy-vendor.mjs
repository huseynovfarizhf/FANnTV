import { mkdir, copyFile, readdir } from 'node:fs/promises';
await mkdir('static/vendor', { recursive: true });
await copyFile('node_modules/hls.js/dist/hls.min.js', 'static/vendor/hls.min.js');
await copyFile('node_modules/hls.js/LICENSE', 'static/vendor/HLS-LICENSE');
for (const family of ['dm-sans', 'manrope']) {
  const root = `node_modules/@fontsource/${family}`;
  const weights = ['400','500','600','700','800'];
  for (const file of await readdir(`${root}/files`)) {
    if (file.startsWith(`${family}-latin-`) && file.endsWith('-normal.woff2') && weights.some(w => file.includes(`-${w}-`))) {
      await copyFile(`${root}/files/${file}`, `static/vendor/${file}`);
    }
  }
  await copyFile(`${root}/LICENSE`, `static/vendor/${family}-LICENSE`);
}
console.log('Local playback library and fonts copied.');
