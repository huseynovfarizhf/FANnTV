import { chromium } from 'playwright';
import { spawn,execFileSync } from 'node:child_process';
import { mkdtemp,mkdir,rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const temp=await mkdtemp(join(tmpdir(),'fanntv-family-'));
const env={...process.env,FANNTV_DB:join(temp,'test.db')};
execFileSync('python3',['-c',`import server,time;server.initialize();db=server.connect();db.execute("INSERT INTO users(email,name,password,role,created) VALUES(?,?,?,'admin',?)",('admin@example.com','Administrator',server.password_hash('browser-test-password'),time.time()));db.commit()`],{env});
const child=spawn('python3',['server.py','--host','127.0.0.1','--port','3198'],{env,stdio:'pipe'});
const base='http://127.0.0.1:3198';
let browser;
const errors=[];
async function json(response,status=200){assert.equal(response.status(),status,await response.text());return response.json();}
try{
 await new Promise((resolve,reject)=>{child.stdout.once('data',resolve);child.once('error',reject);child.once('exit',c=>reject(new Error('Test server exited '+c)));});
 browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const owner=await browser.newContext({viewport:{width:1440,height:1080},locale:'en-GB'});
 const admin=await browser.newContext({viewport:{width:1440,height:1080},locale:'en-GB'});
 const registration=await json(await owner.request.post(base+'/api/register',{data:{name:'Family Owner',email:'owner@example.com',password:'browser-test-password'}}));
 const ownerId=registration.user.id;
 await json(await admin.request.post(base+'/api/login',{data:{email:'admin@example.com',password:'browser-test-password'}}));
 const page=await admin.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.locator('button[data-view=admin]').first().click();
 await page.locator('#pricing-owner').waitFor();
 const global=page.locator('.pricing-form[data-plan=monthly][data-user-id=""]');
 await global.locator('input[name=price]').fill('6.25');
 let response=page.waitForResponse(r=>r.url().endsWith('/api/admin/pricing'));
 await global.getByRole('button',{name:'Save price',exact:true}).click();await json(await response);
 await page.waitForFunction(()=>document.querySelector('.pricing-form[data-plan=monthly][data-user-id=""] input')?.value==='6.25');
 await page.locator('#pricing-owner').selectOption(String(ownerId));
 const personal=page.locator(`#customer-price-forms .pricing-form[data-plan=monthly][data-user-id="${ownerId}"]`);
 await personal.locator('input[name=price]').fill('4.15');
 response=page.waitForResponse(r=>r.url().endsWith('/api/admin/pricing'));
 await personal.getByRole('button',{name:'Save price',exact:true}).click();await json(await response);
 const ownerPage=await owner.newPage();ownerPage.on('pageerror',e=>errors.push(e.message));
 await ownerPage.goto(base);await ownerPage.locator('.topbar').waitFor();
 await ownerPage.locator('.trial-pill').click();
 await ownerPage.locator('button[data-plan=six_month]').waitFor();
 assert.match(await ownerPage.locator('button[data-plan=monthly]').innerText(),/4\.15/);
 assert.match(await ownerPage.locator('button[data-plan=six_month]').innerText(),/Six months/);
 assert(!await ownerPage.locator('.plans').innerText().then(s=>s.includes('$')));
 await ownerPage.locator('button[data-action=close]').click();
 // A confirmed bank receipt activates the six-month package through the real UI.
 const receipt=page.locator('#manual-payment-form');
 await receipt.locator('select[name=user_id]').selectOption(String(ownerId));
 await receipt.locator('select[name=plan]').selectOption('six_month');
 await receipt.locator('input[name=reference]').fill('bank-confirmed-browser-001');
 await receipt.locator('input[name=confirmed]').check();
 await receipt.getByRole('button',{name:'Review and activate',exact:true}).click();
 response=page.waitForResponse(r=>r.url().endsWith('/api/admin/payment'));
 await page.getByRole('button',{name:'Activate service',exact:true}).click();
 await json(await response);
 const me=await json(await owner.request.get(base+'/api/me'));
 assert(me.user.paid_until>Date.now()/1000+175*86400);
 assert(me.user.bonus_until>Date.now()/1000+175*86400);
 await ownerPage.reload();
 await ownerPage.locator('button[data-view=family]').first().click();
 await ownerPage.getByRole('heading',{name:'Family & devices.',exact:true}).waitFor();
 for(const [name,email,bonus] of [['Child One','child1@example.com',false],['Child Two','child2@example.com',true]]){
  await ownerPage.locator('button[data-action=add-member]').click();
  const form=ownerPage.locator('#member-form');
  await form.locator('input[name=name]').fill(name);await form.locator('input[name=email]').fill(email);await form.locator('input[name=password]').fill('browser-test-password');
  if(bonus)await form.locator('input[name=bonus]').check();
  if(!bonus){for(const box of await form.locator('input[name=category]').all()){if(await box.getAttribute('value')!=='Kids')await box.uncheck();}}
  response=ownerPage.waitForResponse(r=>r.url().endsWith('/api/family/member'));
  await form.getByRole('button',{name:'Create sub-user',exact:true}).click();await json(await response);
  await ownerPage.locator('.modal').waitFor({state:'detached'});
  await ownerPage.locator('.family-member').filter({hasText:email}).waitFor();
 }
 await mkdir('test-results',{recursive:true});
 await ownerPage.screenshot({path:'test-results/family-desktop.png',fullPage:true});
 const childOne=await browser.newContext();
 await json(await childOne.request.post(base+'/api/login',{data:{email:'child1@example.com',password:'browser-test-password'}}));
 const channels=await json(await childOne.request.get(base+'/api/channels'));
 assert.deepEqual(channels.channels.map(c=>c.category),['Kids']);
 assert([403,404].includes((await childOne.request.get(base+'/api/play/1')).status()));
 assert.equal((await childOne.request.post(base+'/api/family/member',{data:{}})).status(),403);
 assert.equal((await childOne.request.post(base+'/api/checkout',{data:{plan:'monthly'}})).status(),403);
 const childPage=await childOne.newPage();childPage.on('pageerror',e=>errors.push(e.message));
 await childPage.goto(base);await childPage.locator('.channel-card').waitFor();
 assert.equal(await childPage.locator('.hero').count(),0);
 assert.equal(await childPage.locator('button[data-view=family]').count(),0);
 assert.equal(await childPage.locator('button[data-action=plans]').count(),0);
 const fourth=await browser.newContext();
 await json(await fourth.request.post(base+'/api/login',{data:{email:'owner@example.com',password:'browser-test-password'}}));
 const fifth=await browser.newContext();
 await json(await fifth.request.post(base+'/api/login',{data:{email:'owner@example.com',password:'browser-test-password'}}));
 // Owner + child + two further sign-ins fill the household's four slots.
 const blocked=await browser.newContext();
 assert.equal((await blocked.request.post(base+'/api/login',{data:{email:'owner@example.com',password:'browser-test-password'}})).status(),409);
 await ownerPage.locator('button[data-action=family-refresh]').click();
 await ownerPage.waitForFunction(()=>document.querySelectorAll('.device-row').length===4);
 await ownerPage.locator('button[data-revoke-device]').first().click();
 response=ownerPage.waitForResponse(r=>r.url().endsWith('/api/family/device'));
 await ownerPage.locator('.modal button[data-action=confirm-action]').click();await json(await response);
 await json(await blocked.request.post(base+'/api/login',{data:{email:'owner@example.com',password:'browser-test-password'}}));
 await ownerPage.locator('button[data-action=family-refresh]').click();
 await ownerPage.waitForFunction(()=>document.querySelectorAll('.device-row').length===4);
 await ownerPage.setViewportSize({width:390,height:844});
 assert(await ownerPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await ownerPage.screenshot({path:'test-results/family-mobile.png',fullPage:true});
 await page.screenshot({path:'test-results/billing-admin.png',fullPage:true});
 for(const [code,heading,planLabel] of [['az','Ailə və cihazlar.','Altı ay'],['ru','Семья и устройства.','Шесть месяцев'],['zh-CN','家庭与设备.','六个月']]){
  await ownerPage.locator('.topbar .language-select').selectOption(code);
  await ownerPage.getByRole('heading',{name:heading,exact:true}).waitFor();
  assert(await ownerPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await ownerPage.locator('.trial-pill').click();
  await ownerPage.locator('button[data-plan=six_month]').waitFor();
  assert((await ownerPage.locator('button[data-plan=six_month]').innerText()).includes(planLabel));
  assert((await ownerPage.locator('.plans').innerText()).includes('AZN'));
  assert.equal(await ownerPage.locator('input[autocomplete=cc-number],input[autocomplete=cc-csc]').count(),0);
  await ownerPage.locator('button[data-action=close]').click();
  await ownerPage.screenshot({path:`test-results/family-${code}-mobile.png`,fullPage:true});
 }

 assert.deepEqual(errors,[]);
 console.log('PASS: AZN global/customer pricing, six-month verified receipt activation, family member creation, parental filtering, owner-only controls, four-device cap, device revocation, mobile family view.');
}finally{if(browser)await browser.close();child.kill('SIGTERM');await rm(temp,{recursive:true,force:true});}
