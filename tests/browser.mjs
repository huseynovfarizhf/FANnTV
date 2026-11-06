import { chromium } from 'playwright';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const temp = await mkdtemp(join(tmpdir(), 'fanntv-browser-'));
const env = {...process.env, FANNTV_DB: join(temp, 'test.db')};
execFileSync('python3', ['-c', `import server,time;server.initialize();db=server.connect();db.execute("INSERT INTO users(email,name,password,role,created) VALUES(?,?,?,'admin',?)",('admin@example.com','Administrator',server.password_hash('browser-test-password'),time.time()));db.commit();db.close()`], {env});
const port = 3199;
const base = `http://127.0.0.1:${port}`;
const child = spawn('python3', ['server.py','--host','127.0.0.1','--port',String(port)], {env,stdio:'pipe'});
let browser;
try {
 await new Promise((resolve,reject)=>{child.stdout.once('data',resolve);child.once('error',reject);child.once('exit',code=>reject(new Error('Test server exited: '+code)));});
 browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context = await browser.newContext({viewport:{width:1440,height:1080}});
 const page = await context.newPage();
 const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await mkdir('test-results',{recursive:true});
 await page.goto(base);
 await page.getByRole('button',{name:'Start my free trial'}).waitFor();
 await page.screenshot({path:'test-results/welcome-desktop.png',fullPage:true});
 await page.getByRole('button',{name:'Start my free trial'}).click();
 await page.getByLabel('Your name').fill('Alex Morgan');
 await page.getByLabel('Email address').fill('alex@example.com');
 await page.getByLabel('Password',{exact:true}).fill('browser-test-password');
 await page.getByRole('button',{name:'Start my 6-day trial'}).click();
 await page.getByRole('heading',{name:/Good .*Alex/}).waitFor();
 assert.equal(await page.locator('.channel-card').count(),6);
 await page.screenshot({path:'test-results/home-desktop.png',fullPage:true});
 await page.getByRole('button',{name:'Add to favorites'}).first().click();
 await page.waitForFunction(()=>document.querySelectorAll('.favorite.saved').length===1);
 await page.locator('.sidebar').getByRole('button',{name:'Favorites',exact:true}).click();
 assert.equal(await page.locator('.channel-card').count(),1);
 await page.reload();
 await page.locator('.sidebar').getByRole('button',{name:'Favorites',exact:true}).click();
 assert.equal(await page.locator('.channel-card').count(),1);
 await page.getByRole('button',{name:'Watch NATURE+'}).click();
 await page.waitForFunction(()=>document.querySelector('video')?.readyState>=2);
 await page.locator('video').evaluate(async v=>{v.muted=true;await v.play();});
 await page.waitForFunction(()=>document.querySelector('video')?.currentTime>0);
 await page.getByRole('button',{name:'Close',exact:true}).click();
 await page.locator('.sidebar').getByRole('button',{name:'Live TV',exact:true}).click();
 await page.getByLabel('Search channels').fill('ARENA');
 assert.equal(await page.locator('.channel-card').count(),1);
 await page.getByLabel('Search channels').fill('nothing matches');
 await page.getByRole('heading',{name:'No channels found'}).waitFor();
 await page.getByRole('button',{name:'Explore all channels'}).click();
 await page.getByRole('button',{name:'Toggle TV mode'}).click();
 assert(await page.locator('.tv-mode').count());
 await page.keyboard.press('ArrowRight');
 assert(await page.evaluate(()=>document.activeElement.tagName==='BUTTON'));
 await page.getByRole('button',{name:'Toggle TV mode'}).click();
 await page.locator('.sidebar').getByRole('button',{name:'Settings',exact:true}).click();
 await page.locator('#content').getByRole('button',{name:'Explore plans',exact:true}).click();
 await page.locator('button[data-plan=yearly]').click();
 await page.getByRole('button',{name:'Payments not connected yet'}).click();
 await page.getByRole('alert').filter({hasText:/Payments are not connected/}).waitFor();
 await page.screenshot({path:'test-results/plans.png',fullPage:true});
 await page.getByRole('button',{name:'Close',exact:true}).click();
 // Mobile layout and overflow check.
 await page.setViewportSize({width:390,height:844});
 await page.locator('.mobile-nav').getByRole('button',{name:'Home',exact:true}).click();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await page.screenshot({path:'test-results/home-mobile.png',fullPage:true});
 await page.locator('.mobile-nav').getByRole('button',{name:'Profile',exact:true}).click();
 await page.getByRole('button',{name:'Sign out',exact:true}).click();
 await page.getByRole('button',{name:'I already have an account'}).click();
 await page.getByLabel('Email address').fill('admin@example.com');
 await page.getByLabel('Password',{exact:true}).fill('browser-test-password');
 await page.getByRole('button',{name:'Sign in',exact:true}).click();
 await page.getByRole('heading',{name:/Good .*Administrator/}).waitFor();
 await page.setViewportSize({width:1440,height:1080});
 await page.getByRole('button',{name:'Admin dashboard',exact:true}).click();
 await page.getByRole('heading',{name:'Your control room.'}).waitFor();
 await page.getByRole('button',{name:'Suspend',exact:true}).click();
 await page.locator('td .badge.danger').waitFor();
 await page.getByRole('button',{name:'Restore',exact:true}).click();
 await page.waitForFunction(()=>document.querySelectorAll('td .badge.danger').length===0);
 await page.getByLabel('WORLD NEWS stream URL').fill('https://example.com/channel.mp4');
 const savedResponse=page.waitForResponse(r=>r.url().endsWith('/api/admin/channel')&&r.request().method()==='POST');
 await page.locator('.channel-form').filter({hasText:'WORLD NEWS'}).getByRole('button',{name:'Save',exact:true}).click();
 assert.equal((await savedResponse).status(),200);
 await page.getByRole('button',{name:'Refresh',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.channel-form[data-id="2"] input[name=stream]')?.value==='https://example.com/channel.mp4');
 await page.screenshot({path:'test-results/admin-desktop.png',fullPage:true});

 // Localization: public and authenticated screens, persistence, translated errors,
 // local channel search, Baku regional formatting, and narrow mobile layouts.
 for(const [code,welcome,login,password,signIn,search,nature,adminTitle] of [
  ['az','Pulsuz sınağa başlayın','Artıq hesabım var','Şifrə','Daxil olun','Kanalları axtarın','Təbiət','İdarəetmə mərkəziniz.'],
  ['ru','Начать бесплатный период','У меня уже есть аккаунт','Пароль','Войти','Поиск каналов','Природа','Ваш центр управления.'],
  ['zh-CN','开始免费试用','我已有账户','密码','登录','搜索频道','自然','您的控制中心.'],
 ]) {
  const localized=await browser.newContext({viewport:{width:390,height:844},locale:code});
  const lp=await localized.newPage();
  lp.on('pageerror',e=>errors.push(e.message));
  await lp.goto(base);
  await lp.getByRole('button',{name:welcome,exact:true}).waitFor();
  assert.equal(await lp.locator('html').getAttribute('lang'),code);
  assert(await lp.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await lp.getByRole('button',{name:login,exact:true}).click();
  await lp.locator('input[name=email]').fill('admin@example.com');
  await lp.getByLabel(password,{exact:true}).fill('wrong-password');
  await lp.getByRole('button',{name:signIn,exact:true}).click();
  await lp.waitForFunction(()=>document.querySelector('.form-error')?.textContent.trim().length>0);
  assert(!await lp.locator('.form-error').textContent().then(t=>t.includes('Email or password')));
  await lp.getByLabel(password,{exact:true}).fill('browser-test-password');
  await lp.getByRole('button',{name:signIn,exact:true}).click();
  await lp.locator('.topbar').waitFor();
  await lp.getByLabel(search,{exact:true}).fill(nature);
  assert.equal(await lp.locator('.channel-card').count(),1);
  await lp.screenshot({path:`test-results/home-${code}-mobile.png`,fullPage:true});
  assert(await lp.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await lp.locator('.topbar .language-select').selectOption('en');
  await lp.getByLabel('Search channels',{exact:true}).waitFor();
  await lp.locator('.topbar .language-select').selectOption(code);
  await lp.reload();
  await lp.locator('.topbar').waitFor();
  assert.equal(await lp.locator('html').getAttribute('lang'),code);
  assert.equal(await lp.evaluate(()=>localStorage.getItem('fanntv-language')),code);
  await lp.setViewportSize({width:1440,height:1080});
  await lp.locator('button[data-view=admin]').first().click();
  await lp.getByRole('heading',{name:adminTitle,exact:true}).waitFor();
  const region=await lp.evaluate(()=>{
   const options={day:'numeric',month:'numeric',year:'numeric',timeZone:'Asia/Baku'};
   const formatter=new Intl.DateTimeFormat(locales[language],options);
   return {formatted:regionalDate(new Date('2026-11-06T22:30:00Z'),options),expected:formatter.format(new Date('2026-11-06T22:30:00Z')),parts:formatter.formatToParts(new Date('2026-11-06T22:30:00Z'))};
  });
  assert.equal(region.formatted,region.expected);
  assert.equal(Number(region.parts.find(p=>p.type==='day').value),7);
  assert.equal(Number(region.parts.find(p=>p.type==='month').value),11);
  assert.equal(Number(region.parts.find(p=>p.type==='year').value),2026);
  await lp.screenshot({path:`test-results/admin-${code}.png`,fullPage:true});
  await localized.close();
 }
 assert.deepEqual(errors,[]);
 console.log('PASS: signup, persistent favorites, video playback, channel search, TV navigation, billing state, mobile layout, admin suspension, channel editing. All four languages, translated errors/search, saved language preferences, and Azerbaijan timezone passed. No browser errors.');
} finally {
 if(browser)await browser.close();
 child.kill('SIGTERM');
 await rm(temp,{recursive:true,force:true});
}
