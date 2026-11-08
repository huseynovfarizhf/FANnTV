import {spawn} from 'node:child_process';
import http from 'node:http';
import {mkdtemp,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import assert from 'node:assert/strict';
const temp=await mkdtemp(join(tmpdir(),'fanntv-gunicorn-'));
const env={...process.env,FANNTV_ENV:'production',FANNTV_DB:join(temp,'app.db'),PUBLIC_URL:'https://app.example',FANNTV_SMTP_HOST:'smtp.example',FANNTV_SMTP_FROM:'noreply@app.example'};
const python=process.env.FANNTV_TEST_PYTHON||'.venv/bin/python';
const child=spawn(python,['-m','gunicorn','--config','deploy/gunicorn.conf.py','--bind','127.0.0.1:3196','--worker-tmp-dir',temp,'wsgi:application'],{env,stdio:'pipe'});
let logs='';child.stderr.on('data',s=>logs+=s);child.stdout.on('data',s=>logs+=s);
async function request(path,options={}){
 return new Promise((resolve,reject)=>{
  const req=http.request({hostname:'127.0.0.1',port:3196,path,method:options.method||'GET',headers:options.headers||{}},res=>{
   const chunks=[];res.on('data',s=>chunks.push(s));res.on('end',()=>{const body=Buffer.concat(chunks);resolve({status:res.statusCode,ok:res.statusCode<400,headers:{get:key=>res.headers[key]},json:async()=>JSON.parse(body),text:async()=>body.toString(),arrayBuffer:async()=>body});});
  });req.on('error',reject);if(options.body)req.write(options.body);req.end();
 });
}
async function ready(){for(let i=0;i<80;i++){try{const r=await request('/healthz');if(r.ok)return;}catch{}if(child.exitCode!==null)throw new Error('Gunicorn did not start: '+logs);await new Promise(r=>setTimeout(r,100));}throw new Error('Gunicorn readiness timeout: '+logs);}
try{
 await ready();
 assert.equal((await request('/readyz')).status,200);
 const headers={Host:'app.example','X-Forwarded-Proto':'https','X-Forwarded-For':'203.0.113.50'};
 const me=await request('/api/me',{headers});assert.equal(me.status,200,await me.text());const data=await me.json();assert.equal(data.currency,'AZN');
 assert(me.headers.get('content-security-policy').includes("script-src 'self'"));assert.equal(me.headers.get('strict-transport-security'),'max-age=31536000');
 assert.equal((await request('/api/me')).status,400);
 assert.equal((await request('/api/login',{method:'POST',headers:{...headers,'Content-Type':'application/json',Origin:'https://attacker.example'},body:'{}'})).status,403);
 const responses=await Promise.all(Array.from({length:24},()=>request('/api/me',{headers})));assert(responses.every(r=>r.status===200));await Promise.all(responses.map(r=>r.arrayBuffer()));
 assert(!logs.includes('203.0.113.50'));
 console.log('PASS: actual Gunicorn two-worker production startup, health/readiness, proxy HTTPS/host/origin enforcement, security headers,24 concurrent requests. No SMTP network or payments exercised.');
}finally{child.kill('SIGTERM');await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);setTimeout(()=>child.kill('SIGKILL'),5000).unref();});await rm(temp,{recursive:true,force:true});}
