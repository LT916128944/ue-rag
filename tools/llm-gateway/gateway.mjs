#!/usr/bin/env node
/**
 * 本地模型名映射网关
 *
 * 解决的问题：Cursor 在请求发出前就会拦截 gpt- / opus- 等前缀的模型名。
 * 做法：Cursor 里配置 custom-* 名字 -> 本网关收到后把 model 字段换成真实名字 -> 转发给中转站
 *      -> 回包里的 model 再换回 custom-*，避免客户端对不上。
 *
 * 边界：网关只做映射，不做准入。
 *   - 带 stripPrefix 的名字：剥前缀转发（绕 Cursor 的前缀拦截）
 *   - 不带前缀的名字：原样透传。用户既然填了真名，说明 Cursor 侧本来不拦，
 *     网关不额外校验、不拒绝、不猜测。未知模型名交给上游判。
 *   - GET /v1/models：别名 ∪ 上游真实模型清单，避免真名在客户端"查不到"。
 *
 * 用法： node gateway.mjs [--config path] [--port n]
 * 零依赖，仅需 Node >= 18（本项目用 Node 22）。
 */
import http from 'node:http';
import zlib from 'node:zlib';
import { Readable } from 'node:stream';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const { gunzipSync, inflateSync, brotliDecompressSync } = zlib;

const HERE = path.dirname(fileURLToPath(import.meta.url));

const DEFAULTS = {
  listen: { host: '127.0.0.1', port: 8787 },
  timeoutMs: 900000,
  // 统一前缀：custom-xxx -> xxx
  stripPrefix: 'custom-',
  // 前缀规则覆盖不了的特殊映射，优先级高于 stripPrefix
  modelAliases: {},
  // 把回包里的 model 改回客户端请求的名字，Cursor 校验更稳
  rewriteResponseModel: true,
  logRequests: true,
  upstream: {
    openai: { baseUrl: 'https://api.openai.com/v1', apiKey: '', authStyle: 'bearer' },
    anthropic: { baseUrl: 'https://api.anthropic.com', apiKey: '', authStyle: 'x-api-key' },
  },
};

/* ------------------------------------------------------------------ config */

function deepMerge(base, override) {
  if (!override || typeof override !== 'object' || Array.isArray(override)) return base;
  const out = Array.isArray(base) ? base.slice() : { ...base };
  for (const [k, v] of Object.entries(override)) {
    out[k] = v && typeof v === 'object' && !Array.isArray(v) ? deepMerge(base?.[k] ?? {}, v) : v;
  }
  return out;
}

export function loadConfig(configPath = process.env.GATEWAY_CONFIG || path.join(HERE, 'config.json')) {
  let raw = {};
  if (fs.existsSync(configPath)) {
    raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } else {
    console.warn(`[warn] 没找到配置文件 ${configPath}，使用默认值`);
  }
  const cfg = deepMerge(DEFAULTS, raw);
  // 环境变量兜底，方便不把 key 写进文件
  if (process.env.UPSTREAM_API_KEY) {
    cfg.upstream.openai.apiKey = process.env.UPSTREAM_API_KEY;
    cfg.upstream.anthropic.apiKey = cfg.upstream.anthropic.apiKey || process.env.UPSTREAM_API_KEY;
  }
  if (process.env.UPSTREAM_BASE_URL) cfg.upstream.openai.baseUrl = process.env.UPSTREAM_BASE_URL;
  if (process.env.ANTHROPIC_BASE_URL) cfg.upstream.anthropic.baseUrl = process.env.ANTHROPIC_BASE_URL;
  return cfg;
}

/* ----------------------------------------------------------------- mapping */

/**
 * 模型名映射。只做「改写」，绝不做「校验」。
 *
 * 规则优先级：modelAliases 精确匹配 > stripPrefix 前缀剥离 > 原样透传。
 * 不带前缀的名字一律原样透传：用户自己填的真名（gpt-4o 之类）说明 Cursor 侧
 * 本来就不会拦，网关没有任何理由替它拦。网关不认识的模型名不是错误。
 */
export function resolveUpstreamModel(model, cfg) {
  if (typeof model !== 'string' || !model) return model;
  const aliased = cfg.modelAliases?.[model];
  if (typeof aliased === 'string' && aliased) return aliased;
  const prefix = cfg.stripPrefix;
  if (prefix && model.startsWith(prefix)) {
    const stripped = model.slice(prefix.length);
    if (stripped) return stripped;
  }
  return model;
}

function replaceModelTokens(text, from, to) {
  if (!from || from === to) return text;
  return text
    .split(`"model":"${from}"`).join(`"model":"${to}"`)
    .split(`"model": "${from}"`).join(`"model": "${to}"`)
    .split(`"model" : "${from}"`).join(`"model" : "${to}"`);
}

function rewriteJsonModel(text, from, to) {
  try {
    const obj = JSON.parse(text);
    if (obj && typeof obj === 'object' && obj.model === from) {
      obj.model = to;
      return JSON.stringify(obj);
    }
  } catch {
    /* 不是合法 JSON，退化成文本替换 */
  }
  return replaceModelTokens(text, from, to);
}

/* -------------------------------------------------------------------- http */

function pickUpstreamKind(pathname, cfg, explicit) {
  if (explicit && cfg.upstream?.[explicit]) return explicit;
  if (/^\/v1\/messages(\/|$)/.test(pathname)) return 'anthropic';
  if (/^\/v1\/complete(\/|$)/.test(pathname)) return 'anthropic';
  return 'openai';
}

function joinUrl(baseUrl, pathname, search) {
  const base = String(baseUrl || '').replace(/\/+$/, '');
  let p = pathname || '/';
  // 中转站常把 /v1 写在 base 里，避免出现 /v1/v1
  if (/\/v1$/.test(base) && p.startsWith('/v1/')) p = p.slice(3);
  return base + p + (search || '');
}

function authHeaders(upstream, extra = {}) {
  const out = { ...extra };
  const key = upstream?.apiKey;
  if (!key) return out;
  if (upstream.authStyle === 'x-api-key') {
    out['x-api-key'] = key;
    out['anthropic-version'] = out['anthropic-version'] || '2023-06-01';
  } else if (upstream.authStyle === 'both') {
    out['authorization'] = `Bearer ${key}`;
    out['x-api-key'] = key;
  } else {
    out['authorization'] = `Bearer ${key}`;
  }
  return out;
}

/**
 * 向上游要模型清单。失败只返回空数组，绝不因此让客户端拿不到模型列表。
 * 冷连接 + TLS 握手有时要好几秒，超时给宽一点，别让清单因为慢而消失。
 * 带 60s 缓存：Cursor 会反复拉清单，不必每次都打上游。
 */
const modelsCache = new Map();

async function fetchUpstreamModels(upstream, search, cacheKey) {
  const target = joinUrl(upstream?.baseUrl, '/v1/models', search);
  if (!upstream?.baseUrl) return [];
  const hit = modelsCache.get(cacheKey);
  if (hit && Date.now() - hit.at < 60000) return hit.models;
  const r = await fetch(target, {
    headers: authHeaders(upstream, { accept: 'application/json' }),
    signal: AbortSignal.timeout(20000),
  });
  if (!r.ok) throw new Error(`upstream /v1/models ${r.status}`);
  const j = await r.json();
  const list = Array.isArray(j?.data) ? j.data : [];
  const models = list
    .filter((m) => m && typeof m.id === 'string' && m.id)
    .map((m) => ({ id: m.id, object: 'model', owned_by: m.owned_by || 'upstream' }));
  modelsCache.set(cacheKey, { at: Date.now(), models });
  return models;
}

const HOP_BY_HOP = new Set([
  'host', 'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade',
]);

function buildUpstreamHeaders(req, upstream) {
  const out = {};
  for (const [k, v] of Object.entries(req.headers)) {
    const lk = k.toLowerCase();
    if (HOP_BY_HOP.has(lk)) continue;
    if (lk === 'authorization' || lk === 'x-api-key') continue; // 用网关自己的 key
    if (lk === 'content-length') continue;                      // 让 fetch 重算
    if (lk === 'accept-encoding') continue;                     // 不压缩，才能改写 model
    out[lk] = v;
  }
  return authHeaders(upstream, out);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function sendJson(res, status, obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': body.length });
  res.end(body);
}

async function pipeRewrittenSse(res, body, from, to) {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buf = '';
  const flushLine = (line) => encoder.encode(replaceModelTokens(line, from, to));

  for await (const chunk of body) {
    buf += decoder.decode(chunk, { stream: true });
    let idx;
    let out = [];
    while ((idx = buf.indexOf('\n')) !== -1) {
      const line = buf.slice(0, idx + 1);
      buf = buf.slice(idx + 1);
      out.push(flushLine(line));
    }
    if (out.length) res.write(Buffer.concat(out));
  }
  buf += decoder.decode();
  if (buf) res.write(Buffer.from(flushLine(buf)));
  res.end();
}

export function createGateway(cfg) {
  const server = http.createServer(async (req, res) => {
    const started = Date.now();
    let logLine = `${req.method} ${req.url}`;

    try {
      const url = new URL(req.url, 'http://127.0.0.1');
      const pathname = url.pathname;

      if (pathname === '/health') {
        return sendJson(res, 200, {
          ok: true,
          stripPrefix: cfg.stripPrefix,
          aliases: Object.keys(cfg.modelAliases || {}),
          upstream: {
            openai: cfg.upstream.openai.baseUrl,
            anthropic: cfg.upstream.anthropic.baseUrl,
          },
        });
      }

      // 模型清单：别名 + 上游真实模型，取并集。
      // 只列别名会让「不带 custom- 前缀的真名」在 Cursor 里查不到而被当成不存在，
      // 等于网关替 Cursor 拦了一道，这里必须把上游清单原样带上。
      if (req.method === 'GET' && /^\/v1\/models\/?$/.test(pathname)) {
        const kind = pickUpstreamKind(pathname, cfg, url.searchParams.get('upstream'));
        const upstream = cfg.upstream[kind];
        let upstreamModels = [];
        let upstreamOk = false;
        try {
          upstreamModels = await fetchUpstreamModels(upstream, url.search, `${kind}:${url.search}`);
          upstreamOk = true;
        } catch (e) {
          // 上游列不出来不影响转发，只是清单里没有上游模型
          console.warn(`[warn] 取上游模型清单失败（${e?.message || e}），只返回别名`);
        }
        const aliases = cfg.modelAliases || {};
        const seen = new Set();
        const data = [];
        for (const [id, ownedBy] of [
          ...Object.keys(aliases).map((id) => [id, 'local-gateway']),
          ...upstreamModels.map((m) => [m.id, m.owned_by]),
        ]) {
          if (seen.has(id)) continue;
          seen.add(id);
          data.push({ id, object: 'model', owned_by: ownedBy });
        }
        return sendJson(res, 200, { object: 'list', data, upstream_ok: upstreamOk });
      }

      const kind = pickUpstreamKind(pathname, cfg, url.searchParams.get('upstream'));
      const upstream = cfg.upstream[kind];

      let rawBody = await readBody(req);
      let clientModel = null;
      let upstreamModel = null;

      const ctype = String(req.headers['content-type'] || '').toLowerCase();
      if (rawBody.length && ctype.includes('json')) {
        let parsed = null;
        try {
          parsed = JSON.parse(rawBody.toString('utf8'));
        } catch {
          parsed = null;
        }
        if (parsed && typeof parsed.model === 'string') {
          clientModel = parsed.model;
          upstreamModel = resolveUpstreamModel(clientModel, cfg);
          if (upstreamModel !== clientModel) {
            parsed.model = upstreamModel;
            rawBody = Buffer.from(JSON.stringify(parsed), 'utf8');
          }
        }
      }

      const target = joinUrl(upstream.baseUrl, pathname, url.search);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(new Error('gateway timeout')), cfg.timeoutMs);
      // 只在客户端真的提前断开时中断上游；用 res 而不是 req，
      // 因为 req 的 close 在请求体读完就会触发，会把正常的流式响应误杀。
      res.on('close', () => {
        if (!res.writableEnded) controller.abort(new Error('client closed'));
      });

      let upResp;
      try {
        upResp = await fetch(target, {
          method: req.method,
          headers: buildUpstreamHeaders(req, upstream),
          body: req.method === 'GET' || req.method === 'HEAD' ? undefined : rawBody,
          signal: controller.signal,
          redirect: 'follow',
        });
      } finally {
        clearTimeout(timer);
      }

      const respCtype = String(upResp.headers.get('content-type') || '').toLowerCase();
      const respEnc = String(upResp.headers.get('content-encoding') || '').toLowerCase();
      const needRewrite = cfg.rewriteResponseModel && clientModel && upstreamModel && clientModel !== upstreamModel;

      res.statusCode = upResp.status;
      for (const [k, v] of upResp.headers) {
        const lk = k.toLowerCase();
        if (HOP_BY_HOP.has(lk)) continue;
        // 响应体要改写就不能是压缩的：下面会解压，所以去掉这两个头
        if (needRewrite && (lk === 'content-encoding' || lk === 'content-length')) continue;
        try {
          res.setHeader(k, v);
        } catch {
          /* 忽略非法头 */
        }
      }

      logLine +=
        clientModel === null
          ? `  [${kind}] ${upResp.status}`
          : clientModel === upstreamModel
            ? `  ${clientModel} (透传)  [${kind}] ${upResp.status}`
            : `  ${clientModel} -> ${upstreamModel}  [${kind}] ${upResp.status}`;

      if (!upResp.body) return res.end();

      // 上游无视 identity 仍压缩时，先解压再改写
      const bodyStream = async () => {
        if (!needRewrite || !respEnc || respEnc === 'identity') return upResp.body;
        const raw = Buffer.from(await upResp.arrayBuffer());
        let plain = raw;
        try {
          if (respEnc === 'gzip' || respEnc === 'x-gzip') plain = gunzipSync(raw);
          else if (respEnc === 'deflate') plain = inflateSync(raw);
          else if (respEnc === 'br') plain = brotliDecompressSync(raw);
          else return Readable.from([raw]);
        } catch {
          return Readable.from([raw]); // 解不开就原样给出去
        }
        return Readable.from([plain]);
      };

      const out = await bodyStream();

      if (respCtype.includes('text/event-stream')) {
        if (needRewrite) await pipeRewrittenSse(res, out, upstreamModel, clientModel);
        else for await (const chunk of out) res.write(chunk);
        res.end();
        return;
      }

      if (needRewrite && respCtype.includes('json')) {
        const chunks = [];
        for await (const chunk of out) chunks.push(chunk);
        res.end(rewriteJsonModel(Buffer.concat(chunks).toString('utf8'), upstreamModel, clientModel));
        return;
      }

      for await (const chunk of out) res.write(chunk);
      res.end();
    } catch (err) {
      const aborted = err?.name === 'AbortError';
      logLine += `  ERROR ${err?.message || err}`;
      if (!res.headersSent) {
        sendJson(res, aborted ? 504 : 502, {
          error: { type: 'gateway_error', message: String(err?.message || err) },
        });
      } else {
        try { res.end(); } catch { /* noop */ }
      }
    } finally {
      if (cfg.logRequests) {
        console.log(`[${new Date().toLocaleTimeString()}] ${logLine}  ${Date.now() - started}ms`);
      }
    }
  });

  // 长流式请求别被默认超时掐断
  server.requestTimeout = 0;
  server.headersTimeout = 120000;
  server.keepAliveTimeout = 65000;
  return server;
}

/* -------------------------------------------------------------------- main */

/**
 * 端口被占用时给出可操作的提示，而不是抛未捕获的 'error' 事件。
 * 如果占用者就是本网关（/health 正常），按「已在运行」处理并退出 0，
 * 这样重复双击 start.bat 不会刷一屏堆栈。
 */
async function handleListenError(err, host, port) {
  if (err?.code === 'EADDRINUSE') {
    const probe = `http://${host}:${port}/health`;
    try {
      const r = await fetch(probe, { signal: AbortSignal.timeout(2000) });
      // 必须是我们自己的 /health（返回 ok:true 的 JSON），别的服务占端口不算
      const info = r.ok ? await r.json().catch(() => null) : null;
      if (info && info.ok === true) {
        console.log(`[已在运行] 端口 ${host}:${port} 上已有网关实例，无需重复启动。`);
        console.log(`           ${probe}`);
        if (info?.upstream) {
          console.log(`           当前实例 openai 上游   : ${info.upstream.openai}`);
          console.log(`           当前实例 anthropic 上游: ${info.upstream.anthropic}`);
        }
        console.log('           注意：新改的 config.json 不会生效，需要先停掉旧实例再启动。');
        console.log(`           停掉旧实例：netstat -ano | findstr :${port}  然后  taskkill /PID <pid> /F`);
        return process.exit(0);
      }
    } catch {
      /* 不是本网关，走下面的通用提示 */
    }
    console.error(`[错误] 端口 ${host}:${port} 已被其他程序占用，且 /health 无响应。`);
    console.error(`       查看占用进程：netstat -ano | findstr :${port}`);
    console.error(`       换端口：改 config.json 的 listen.port，或启动时加 --port <新端口>，`);
    console.error('       Cursor 里的 base URL 同步改成新端口。');
    return process.exit(1);
  }
  console.error(`[错误] 网关启动失败：${err?.message || err}`);
  return process.exit(1);
}

function main() {
  const args = process.argv.slice(2);
  const ci = args.indexOf('--config');
  const configPath = ci !== -1 ? args[ci + 1] : undefined;
  const pi = args.indexOf('--port');
  const portOverride = pi !== -1 ? Number(args[pi + 1]) : undefined;

  const cfg = loadConfig(configPath);
  if (Number.isInteger(portOverride) && portOverride > 0 && portOverride < 65536) {
    cfg.listen.port = portOverride;
  }
  const server = createGateway(cfg);
  const { host, port } = cfg.listen;

  // 没有这个监听，EADDRINUSE 会以未捕获的 'error' 事件炸出整屏堆栈。
  server.on('error', (err) => {
    handleListenError(err, host, port).catch((e) => {
      console.error(`[错误] 网关启动失败：${e?.message || e}`);
      process.exit(1);
    });
  });

  server.listen(port, host, () => {
    console.log(`llm-gateway 已启动: http://${host}:${port}`);
    console.log(`  openai 上游   : ${cfg.upstream.openai.baseUrl}  (key ${cfg.upstream.openai.apiKey ? '已配置' : '未配置'})`);
    console.log(`  anthropic 上游: ${cfg.upstream.anthropic.baseUrl}  (key ${cfg.upstream.anthropic.apiKey ? '已配置' : '未配置'})`);
    console.log(`  前缀规则      : ${cfg.stripPrefix}* -> *`);
    const aliases = Object.entries(cfg.modelAliases || {});
    if (aliases.length) console.log(`  别名映射      : ${aliases.map(([a, b]) => `${a} -> ${b}`).join(', ')}`);
    console.log(`  Cursor 里填   : base url = http://${host}:${port}/v1`);
  });

  const shutdown = () => server.close(() => process.exit(0));
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
