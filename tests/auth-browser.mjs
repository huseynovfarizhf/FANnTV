import {chromium} from 'playwright';
import {spawn} from 'node:child_process';
import {mkdtemp,readFile,rm,mkdir} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import assert from 'node:assert/strict';
const temp=await mkdtemp(join(tmpdir(),'fanntv-auth-'));
const mailFile=join(temp,'mail.jsonl');
const env={...process.env,FANNTV_DB:join(temp,'app.db'),FANNTV_ENV:'development',FANNTV_TEST_MAIL:mailFile};
// Fixture intercepts SMTP delivery; real tokens still originate in account_security.
const fixture=`import os,json,server,account_security
account_security.email_delivery_ready=lambda: True
def capture(email,purpose,token):
 with open(os.environ['FANNTV_TEST_MAIL'],'a') as f: f.write(json.dumps({'email':email,'purpose':purpose,'token':token})+'\\n')
account_security.send_account_email=capture
server.initialize()
server.ThreadingHTTPServer(('127.0.0.1',3197),server.Handler).serve_forever()
`;
const child=spawn('python3',['-u','-c',fixture],{env,stdio:'pipe'});
let browser;
const base='http://127.0.0.1:3197';
async function lastToken(purpose){for(let i=0;i<30;i++){try{const rows=(await readFile(mailFile,'utf8')).trim().split('\n').map(JSON.parse);const row=rows.filter(r=>r.purpose===purpose).at(-1);if(row)return row.token;}catch{}await new Promise(r=>setTimeout(r,100));}throw new Error('Fixture email was not captured');}
try{
 browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:1440,height:1080},locale:'en-GB'});
 const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(let n=0;n<50;n++){try{await context.request.get(base+'/api/me');break;}catch{await new Promise(r=>setTimeout(r,100));}}
 await page.goto(base);await page.getByRole('button',{name:'Start my free trial',exact:true}).click();
 await page.getByLabel('Your name',{exact:true}).fill('Security Tester');
 await page.getByLabel('Email address',{exact:true}).fill('security@example.com');
 await page.getByLabel('Password',{exact:true}).fill('initial-test-password');
 await page.getByRole('button',{name:'Start my 6-day trial',exact:true}).click();await page.locator('.topbar').waitFor();
 const verifyToken=await lastToken('verify');
 await page.goto(base+'/#verify='+verifyToken);
 await page.locator('button[data-action=verify-email]').waitFor();
 assert.equal(new URL(page.url()).hash,'');
 await page.locator('button[data-action=verify-email]').click();
 await page.waitForFunction(()=>!document.querySelector('.modal button[data-action=verify-email]'));
 let me=await (await context.request.get(base+'/api/me')).json();assert(me.user.email_verified);
 await page.getByRole('button',{name:'Continue',exact:true}).click();
 await page.locator('.sidebar button[data-view=settings]').first().click();
 await page.getByRole('button',{name:'Change password',exact:true}).click();
 const change=page.locator('#change-password-form');
 await change.locator('[name=current_password]').fill('initial-test-password');await change.locator('[name=password]').fill('changed-test-password');await change.locator('[name=confirm_password]').fill('changed-test-password');
 const passwordResponse=page.waitForResponse(r=>r.url().endsWith('/api/account/password'));
 await change.getByRole('button',{name:'Update password',exact:true}).click();
 const updated=await passwordResponse;assert.equal(updated.status(),200,await updated.text());
 await page.locator('#auth-form[data-mode=login]').waitFor();
 me=await(await context.request.get(base+'/api/me')).json();assert.equal(me.user,null);
 await page.getByRole('button',{name:'Forgot password?',exact:true}).click();
 const forgot=page.locator('#forgot-password-form');
 await forgot.locator('[name=email]').fill('security@example.com');
 await forgot.locator('button[type=submit]').click();
 await page.getByRole('heading',{name:'Check your inbox',exact:true}).waitFor();
 const resetToken=await lastToken('reset');
 await page.goto(base+'/#reset='+resetToken);
 const reset=page.locator('#reset-password-form');await reset.waitFor();assert.equal(new URL(page.url()).hash,'');
 assert(!JSON.stringify(await page.evaluate(()=>({...localStorage}))).includes(resetToken));
 await reset.locator('[name=password]').fill('recovered-test-password');await reset.locator('[name=confirm_password]').fill('recovered-test-password');
 await reset.locator('button[type=submit]').click();
 await page.locator('#auth-form[data-mode=login]').waitFor();
 await page.locator('#auth-form [name=email]').fill('security@example.com');await page.locator('#auth-form [name=password]').fill('recovered-test-password');await page.locator('#auth-form button[type=submit]').click();
 await page.locator('.topbar').waitFor();
 await mkdir('test-results',{recursive:true});await page.locator('.sidebar button[data-view=settings]').first().click();await page.screenshot({path:'test-results/account-security.png',fullPage:true});
 assert.deepEqual(errors,[]);
 console.log('PASS: verification, fragment-token removal, password change and session revocation, recovery email, single-use reset, recovered login. SMTP is fixture-only.');
}finally{if(browser)await browser.close();child.kill('SIGTERM');await rm(temp,{recursive:true,force:true});}
