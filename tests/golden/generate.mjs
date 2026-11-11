// Records the requests the TypeScript SDK sends for every scenario in scenarios.json.
//
//   PINGROOM_TS_SDK=/path/to/sdk/dist/index.js node tests/golden/generate.mjs
//
// A maintainer tool: it needs a built checkout of the TypeScript SDK (@pingroom/sdk), which
// this repository does not contain. The output, ts-requests.json, is committed, and
// tests/test_golden.py replays every scenario through this SDK and compares.

import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const sdkPath = process.env.PINGROOM_TS_SDK;
if (!sdkPath) {
  console.error('Set PINGROOM_TS_SDK to the built TypeScript SDK entry, e.g. ../sdk/dist/index.js');
  process.exit(1);
}
const { PingRoom, sendIncomingWebhook, VERSION } = await import(pathToFileURL(sdkPath).href);
const scenarios = JSON.parse(readFileSync(join(here, 'scenarios.json'), 'utf8'));
const out = [];
for (const sc of scenarios) {
  const calls = [];
  const fetchMock = async (url, init) => {
    calls.push({ url: String(url), init });
    const body = sc.response.body === null ? '' : JSON.stringify(sc.response.body);
    return new Response(body, { status: sc.response.status, headers: sc.call === 'attachments.content' ? { 'Content-Type': 'application/pdf' } : {} });
  };
  const pr = new PingRoom({ token: 'tok_test', fetch: fetchMock });
  const a = sc.args;
  const idem = a.idempotency_key !== undefined ? { idempotencyKey: a.idempotency_key } : {};
  let error = null;
  try {
    switch (sc.call) {
      case 'broadcast': {
        const p = { message: a.message };
        for (const k of ['title', 'action_number', 'action_icon', 'data', 'correlation_id', 'reply_to', 'tags', 'attachment_ids', 'requires_ack', 'ack_mode', 'is_urgent', 'ack_timeout_seconds']) if (a[k] !== undefined) p[k] = a[k];
        // key order: the wire order of the Python SDK (declared order), replace_key last
        const ordered = {};
        for (const k of ['message', 'title', 'action_number', 'action_icon', 'data', 'correlation_id', 'reply_to', 'tags', 'attachment_ids', 'requires_ack', 'ack_mode', 'is_urgent', 'ack_timeout_seconds']) if (p[k] !== undefined) ordered[k] = p[k];
        if (a.replace_key !== undefined) ordered.replaceKey = a.replace_key;
        await pr.broadcast(a.invite_code, { ...ordered, ...idem });
        break;
      }
      case 'notifications.list': await pr.notifications.list(a); break;
      case 'notifications.get': await pr.notifications.getNotification(a.notification_id); break;
      case 'notifications.wait': await pr.notifications.wait(a); break;
      case 'notifications.wait_for_acknowledgement': await pr.notifications.waitForAcknowledgement(a.notification_id, { timeout: a.timeout }); break;
      case 'notifications.replace': { const p = { message: a.message }; if ('title' in a) p.title = a.title; if ('data' in a) p.data = a.data; await pr.notifications.replace(a.invite_code, a.notification_id, p); break; }
      case 'notifications.withdraw': await pr.notifications.withdraw(a.invite_code, a.notification_id); break;
      case 'approvals.request': { const { invite_code, ...rest } = a; const { idempotency_key, ...body } = rest; await pr.approvals.request(invite_code, { ...body, ...idem }); break; }
      case 'approvals.get': await pr.approvals.get(a.approval_id); break;
      case 'approvals.wait': await pr.approvals.wait(a.approval_id, { timeout: a.timeout }); break;
      case 'approvals.wait_for_decision': await pr.approvals.waitForDecision(a.approval_id); break;
      case 'questions.ask': { const { invite_code, idempotency_key, ...body } = a; await pr.questions.ask(invite_code, { ...body, ...idem }); break; }
      case 'questions.get': await pr.questions.get(a.question_id); break;
      case 'questions.wait': await pr.questions.wait(a.question_id, { timeout: a.timeout }); break;
      case 'questions.wait_for_answer': await pr.questions.waitForAnswer(a.question_id); break;
      case 'questions.list': await pr.questions.list(a); break;
      case 'questions.cancel': await pr.questions.cancel(a.question_id); break;
      case 'handoffs.request_ack': { const input = { prompt: a.prompt }; if (a.target !== undefined) input.target = a.target; if (a.expires_in !== undefined) input.expiresIn = a.expires_in; if (a.urgency !== undefined) input.urgency = a.urgency; if (a.correlation_id !== undefined) input.correlationId = a.correlation_id; if (a.reply_to !== undefined) input.replyTo = a.reply_to; if (a.data !== undefined) input.data = a.data; await pr.handoffs.requestAck({ ...input, ...idem }); break; }
      case 'handoffs.ask': { const input = { prompt: a.prompt, options: a.options }; await pr.handoffs.ask({ ...input, ...idem }); break; }
      case 'handoffs.get': await pr.handoffs.get(a.handoff_id); break;
      case 'handoffs.wait_for_result': await pr.handoffs.waitForResult(a.handoff_id, { timeout: a.timeout }); break;
      case 'handoffs.list': await pr.handoffs.list(a); break;
      case 'metrics.list': await pr.metrics.list(a.invite_code); break;
      case 'metrics.set': { const { invite_code, key, ...input } = a; await pr.metrics.set(invite_code, key, input); break; }
      case 'metrics.delete': await pr.metrics.delete(a.invite_code, a.key); break;
      case 'attachments.manifest': await pr.attachments.manifest(a.attachment_id); break;
      case 'attachments.content': await pr.attachments.content(a.attachment_id); break;
      case 'attachments.delete': await pr.attachments.delete(a.attachment_id); break;
      case 'live.push': await pr.live.push(a.invite_code, a.ping); break;
      case 'live.get': await pr.live.get(a.invite_code, a.correlation_id); break;
      case 'live.claim': await pr.live.claim(a.invite_code, a.notification_id, a.action); break;
      case 'webhook': { const p = { ...a.payload }; if ('replace_key' in p) { p.replaceKey = p.replace_key; delete p.replace_key; } await sendIncomingWebhook(a.url, p, { fetch: fetchMock, ...idem }); break; }
      default: throw new Error('unknown call ' + sc.call);
    }
  } catch (e) { error = String(e && e.message); }
  out.push({ id: sc.id, error, calls: calls.map(({ url, init }) => ({ method: init.method, url, headers: Object.fromEntries(Object.entries(init.headers).map(([k, v]) => [k.toLowerCase(), v])), body: typeof init.body === 'string' ? init.body : null })) });
}
const keep = ['accept', 'authorization', 'content-type', 'idempotency-key'];
const requests = out.map(({ id, error, calls }) => ({
  id,
  error,
  requests: calls.map(({ method, url, headers, body }) => {
    const { pathname, search } = new URL(url);
    return {
      method,
      path: pathname + search,
      headers: Object.fromEntries(keep.filter((name) => name in headers).map((name) => [name, headers[name]])),
      body,
    };
  }),
}));
writeFileSync(
  join(here, 'ts-requests.json'),
  JSON.stringify({ typescript_sdk_version: VERSION, scenarios: requests }, null, 1) + '\n',
);
console.log('recorded', requests.length, 'scenarios from @pingroom/sdk', VERSION);
