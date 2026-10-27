#!/usr/bin/env node
/**
 * 自检：起一个假上游 + 起网关，验证模型名双向改写、流式透传、路径拼接。
 * 运行：node selftest.mjs
 */
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { gzipSync } from 'node:zlib';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GW_PORT = 18787;

let pass = 0;
let fail = 0;
function check(name, cond, extra = '') {
  if (cond) {
    pass++;
    console.log(`  ok   ${name}`);
  } else {
    fail++;
    console.log(`  FAIL ${name} ${extra}`);
  }
}

/* ------------------------------- fake upstream ------------------------------ */
const seen = [];
const upstream = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    seen.push({
      url: req.url,
      model: body.model,
      auth: req.headers.authorization,
      apiKey: req.headers['x-api-key'],
    });

    // 上游模型清单：网关要把它和别名合并后返回给客户端
    if (req.method === 'GET' && /^\/v1\/models\/?$/.test(req.url)) {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(
        JSON.stringify({
          object: 'list',
          data: [
            { id: 'gpt-6.1-sol', object: 'model', owned_by: 'upstream' },
            { id: 'claude-opus-5', object: 'model', owned_by: 'upstream' },
            { id: 'custom-fast', object: 'model', owned_by: 'upstream' },
          ],
        }),
      );
      return;
    }

    if (body.stream) {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(`data: ${JSON.stringify({ id: '1', model: body.model })}\n\n`);
      setTimeout(() => {
        res.write(`data: ${JSON.stringify({ id: '2', model: body.model })}\n\n`);
        res.write('data: [DONE]\n\n');
        res.end();
      }, 30);
      return;
    }

    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ id: 'chatcmpl-x', model: body.model, ok: true }));
  });
});

/* 模拟无视 identity、仍返回 gzip 的上游 */
const gzUpstream = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    const payload = Buffer.from(JSON.stringify({ id: 'gz', model: body.model }), 'utf8');
    res.writeHead(200, { 'content-type': 'application/json', 'content-encoding': 'gzip' });
    res.end(gzipSync(payload));
  });
});

/* ----------------------------------- run ---------------------------------- */
const tmpCfg = path.join(os.tmpdir(), `llm-gateway-selftest-${process.pid}.json`);

await new Promise((r) => upstream.listen(0, '127.0.0.1', r));
const upPort = upstream.address().port;

await new Promise((r) => gzUpstream.listen(0, '127.0.0.1', r));
const gzPort = gzUpstream.address().port;

fs.writeFileSync(
  tmpCfg,
  JSON.stringify(
    {
      listen: { host: '127.0.0.1', port: GW_PORT },
      stripPrefix: 'custom-',
      modelAliases: { 'custom-fast': 'beta-real' },
      rewriteResponseModel: true,
      logRequests: false,
      upstream: {
        openai: { baseUrl: `http://127.0.0.1:${upPort}/v1`, apiKey: 'sk-test', authStyle: 'bearer' },
        anthropic: { baseUrl: `http://127.0.0.1:${upPort}`, apiKey: 'sk-test', authStyle: 'both' },
        gzip: { baseUrl: `http://127.0.0.1:${gzPort}/v1`, apiKey: 'sk-test', authStyle: 'bearer' },
        // 连不上的上游：验证 /v1/models 失败时不会把整个清单搞崩
        deadmodel: { baseUrl: 'http://127.0.0.1:1/v1', apiKey: 'sk-test', authStyle: 'bearer' },
      },
    },
    null,
    2,
  ),
);

const gw = spawn(process.execPath, [path.join(HERE, 'gateway.mjs'), '--config', tmpCfg], {
  stdio: ['ignore', 'pipe', 'pipe'],
});
let gwOut = '';
gw.stdout.on('data', (d) => (gwOut += d));
gw.stderr.on('data', (d) => (gwOut += d));

async function waitReady() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${GW_PORT}/health`);
      if (r.ok) return;
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`网关没起来:\n${gwOut}`);
}

const post = (p, body, headers = {}) =>
  fetch(`http://127.0.0.1:${GW_PORT}${p}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });

try {
  await waitReady();

  console.log('\n[1] 前缀映射 custom-alpha -> alpha');
  {
    const j = await (await post('/v1/chat/completions', { model: 'custom-alpha', messages: [] }, { authorization: 'Bearer sk-cursor-side' })).json();
    check('上游收到 alpha', seen.at(-1).model === 'alpha', `实际 ${seen.at(-1).model}`);
    check('上游用网关 key', seen.at(-1).auth === 'Bearer sk-test', `实际 ${seen.at(-1).auth}`);
    check('回包 model 改回 custom-alpha', j.model === 'custom-alpha', `实际 ${j.model}`);
    check('路径没有 /v1/v1', seen.at(-1).url === '/v1/chat/completions', `实际 ${seen.at(-1).url}`);
  }

  console.log('\n[2] 带点号的版本号 custom-v1.2-pro -> v1.2-pro');
  {
    const j = await (await post('/v1/chat/completions', { model: 'custom-v1.2-pro', messages: [] })).json();
    check('上游收到 v1.2-pro', seen.at(-1).model === 'v1.2-pro', `实际 ${seen.at(-1).model}`);
    check('回包改回 custom-v1.2-pro', j.model === 'custom-v1.2-pro', `实际 ${j.model}`);
  }

  console.log('\n[3] 裸 custom- 不产生空模型名');
  {
    await post('/v1/chat/completions', { model: 'custom-', messages: [] });
    check('原样保留 custom-', seen.at(-1).model === 'custom-', `实际 ${seen.at(-1).model}`);
  }

  console.log('\n[4] 显式别名优先于前缀规则');
  {
    await post('/v1/chat/completions', { model: 'custom-fast', messages: [] });
    check('custom-fast -> beta-real', seen.at(-1).model === 'beta-real', `实际 ${seen.at(-1).model}`);
  }

  console.log('\n[5] 无前缀模型名原样透传');
  {
    const j = await (await post('/v1/chat/completions', { model: 'gamma', messages: [] })).json();
    check('上游收到 gamma', seen.at(-1).model === 'gamma', `实际 ${seen.at(-1).model}`);
    check('回包 model 未动', j.model === 'gamma', `实际 ${j.model}`);
  }

  console.log('\n[6] 流式 SSE：逐块改写、不整体缓冲');
  {
    const t0 = Date.now();
    const r = await post('/v1/chat/completions', { model: 'custom-alpha', stream: true, messages: [] });
    const text = await r.text();
    const firstChunkMs = Date.now() - t0;
    check('上游收到 alpha', seen.at(-1).model === 'alpha', `实际 ${seen.at(-1).model}`);
    check('SSE 里没有真实名 alpha', !/"model":"alpha"/.test(text), text.slice(0, 200));
    check('SSE 里改成 custom-alpha', (text.match(/custom-alpha/g) || []).length === 2, text.slice(0, 200));
    check('[DONE] 保留', text.includes('data: [DONE]'));
    check('首块未被整体缓冲（<1500ms）', firstChunkMs < 1500, `${firstChunkMs}ms`);
  }

  console.log('\n[7] /v1/messages 走 anthropic 上游 + x-api-key');
  {
    const j = await (await post('/v1/messages', { model: 'custom-alpha', max_tokens: 16, messages: [] })).json();
    check('上游收到 alpha', seen.at(-1).model === 'alpha', `实际 ${seen.at(-1).model}`);
    check('用 x-api-key 鉴权', seen.at(-1).apiKey === 'sk-test', `实际 ${seen.at(-1).apiKey}`);
    check('回包改回 custom-alpha', j.model === 'custom-alpha', `实际 ${j.model}`);
  }

  console.log('\n[8] GET /v1/models = 别名 ∪ 上游清单');
  {
    const j = await (await fetch(`http://127.0.0.1:${GW_PORT}/v1/models`)).json();
    check('包含别名 custom-fast', j.data.some((m) => m.id === 'custom-fast'), JSON.stringify(j).slice(0, 200));
    check('包含上游真名 gpt-6.1-sol', j.data.some((m) => m.id === 'gpt-6.1-sol'), JSON.stringify(j).slice(0, 200));
    check('同名不重复', j.data.filter((m) => m.id === 'custom-fast').length === 1, JSON.stringify(j).slice(0, 200));
    check('空 id 被过滤', j.data.every((m) => m.id), JSON.stringify(j).slice(0, 200));
    check('upstream_ok=true', j.upstream_ok === true, JSON.stringify(j).slice(0, 200));
  }

  console.log('\n[9] 上游 4xx 原样透传');
  {
    const r = await fetch(`http://127.0.0.1:${GW_PORT}/nope`, { method: 'GET' });
    check('状态码透传', r.status === 200, `实际 ${r.status}`);
  }

  console.log('\n[10] 上游无视 identity 仍 gzip：解压后仍能改写');
  {
    const j = await (await post('/v1/chat/completions?upstream=gzip', { model: 'custom-alpha', messages: [] })).json();
    check('回包 model 改回 custom-alpha', j.model === 'custom-alpha', JSON.stringify(j));
  }

  console.log('\n[11] 不带前缀的名字不阻拦：任意真名原样透传');
  {
    for (const name of ['gpt-6.1-sol', 'claude-opus-5', 'opus-5', 'some-unknown-model', 'Custom-Mixed']) {
      const r = await post('/v1/chat/completions', { model: name, messages: [] });
      const j = await r.json();
      check(`网关不拒绝 ${name}`, r.status === 200, `状态 ${r.status}`);
      check(`上游收到原名 ${name}`, seen.at(-1).model === name, `实际 ${seen.at(-1).model}`);
      check(`回包 model 未被改动 ${name}`, j.model === name, `实际 ${j.model}`);
    }
  }

  console.log('\n[12] 上游模型清单取不到时，仍然返回别名而不是报错');
  {
    const r = await fetch(`http://127.0.0.1:${GW_PORT}/v1/models?upstream=deadmodel`);
    const j = await r.json();
    check('状态 200', r.status === 200, `实际 ${r.status}`);
    check('upstream_ok=false', j.upstream_ok === false, JSON.stringify(j).slice(0, 200));
    check('别名仍在', j.data.some((m) => m.id === 'custom-fast'), JSON.stringify(j).slice(0, 200));
  }
} finally {
  gw.kill();
  upstream.close();
  gzUpstream.close();
  try {
    fs.unlinkSync(tmpCfg);
  } catch {
    /* noop */
  }
}

console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);
