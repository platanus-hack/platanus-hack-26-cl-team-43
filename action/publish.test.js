// node --test action/publish.test.js  (también lo corre tests/test_publish_js.py)
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { publish, MARKER } = require('./publish.js');

const BOT = 'github-actions[bot]';
const HEAD = 'b'.repeat(40);
const body = (title = 'APROBADO') => `### Cachai: ${title}\n\nalgo\n\n${MARKER}\n`;

function report(outcome = 'approved', markdown = body(outcome === 'approved' ? 'APROBADO' : 'RECHAZADO')) {
  return JSON.stringify({ outcome, public: { summary: `Cachai: ${outcome}`, markdown } });
}

// `github` falso: páginas de comentarios, registro de llamadas y errores a pedido.
function fakeGithub({ pages = [[]], fail = {} } = {}) {
  const calls = { update: [], create: [], checks: [], pagesRead: 0 };
  const maybeFail = (name, args) => {
    const f = fail[name];
    const err = typeof f === 'function' ? f(args) : f;
    if (err) throw Object.assign(new Error(err.message || 'falla'), { status: err.status });
  };
  const rest = {
    issues: {
      listComments: () => {},
      updateComment: async (a) => { maybeFail('update', a); calls.update.push(a); },
      createComment: async (a) => { maybeFail('create', a); calls.create.push(a); },
    },
    checks: { create: async (a) => { maybeFail('checks', a); calls.checks.push(a); } },
  };
  const paginate = async (fn, params, map) => {
    assert.equal(fn, rest.issues.listComments);
    let stop = false;
    const out = [];
    for (const data of pages) {
      calls.pagesRead++;
      out.push(...map({ data }, () => { stop = true; }));
      if (stop) break;
    }
    return out;
  };
  return { github: { rest, paginate }, calls };
}

function fakeCore() {
  const warnings = [];
  const summary = { addRaw() { return summary; }, async write() {} };
  return { warnings, warning: (m) => warnings.push(m), summary };
}

function ctx({ headRepo = 'o/r' } = {}) {
  return {
    repo: { owner: 'o', repo: 'r' },
    payload: { pull_request: { number: 7, head: { sha: HEAD, repo: headRepo === null ? null : { full_name: headRepo } } } },
  };
}

async function run({ pages, fail, rep = report(), headRepo, env = {} } = {}) {
  const { github, calls } = fakeGithub({ pages, fail });
  const core = fakeCore();
  const result = await publish({
    github, core, context: ctx({ headRepo }),
    env: { REPORT: 'r.json', COMMENT: 'true', CHECK_RUN: 'true', AUTHOR: BOT, ...env },
    readFile: () => { if (rep === null) throw new Error('no existe'); return rep; },
  });
  return { result, calls, core };
}

test('ignora un comentario de otro autor con el marcador y crea el propio', async () => {
  const { calls } = await run({ pages: [[{ id: 1, user: { login: 'autor-del-pr' }, body: body() }]] });
  assert.equal(calls.update.length, 0);
  assert.equal(calls.create.length, 1);
});

test('ignora un comentario propio que solo cita el marcador en el medio', async () => {
  const { calls } = await run({ pages: [[{ id: 1, user: { login: BOT }, body: `hola ${MARKER} chau` }]] });
  assert.equal(calls.update.length, 0);
  assert.equal(calls.create.length, 1);
});

test('edita el comentario propio aunque esté en la página 2, y deja de paginar', async () => {
  const other = { id: 1, user: { login: 'x' }, body: 'nada' };
  const own = { id: 42, user: { login: BOT }, body: body() + '\n  ' };
  const { calls, result } = await run({ pages: [[other], [own], [{ id: 99, user: { login: 'x' }, body: 'nada' }]] });
  assert.deepEqual(calls.update.map((u) => u.comment_id), [42]);
  assert.equal(calls.create.length, 0);
  assert.equal(calls.pagesRead, 2);
  assert.equal(result.comment, 'updated');
});

test('un 403 deja un warning sin romper', async () => {
  const { result, core } = await run({ fail: { create: { status: 403, message: 'Resource not accessible' }, checks: { status: 403 } } });
  assert.equal(result.comment, 'failed');
  assert.equal(result.check, 'failed');
  assert.equal(core.warnings.length, 2);
});

test('outcome incomplete da failure en el check run', async () => {
  const { calls } = await run({ rep: report('incomplete', body('SIN VEREDICTO')) });
  assert.equal(calls.checks[0].conclusion, 'failure');
  assert.equal(calls.checks[0].head_sha, HEAD);
});

test('outcome approved da success en el check run', async () => {
  const { calls } = await run();
  assert.equal(calls.checks[0].conclusion, 'success');
});

test('un cuerpo de más de 65536 caracteres se reemplaza por uno mínimo', async () => {
  const own = { id: 5, user: { login: BOT }, body: body() };
  const huge = body('RECHAZADO').replace('algo', 'x'.repeat(70000));
  const { calls, result } = await run({ pages: [[own]], rep: report('rejected', huge) });
  assert.equal(result.comment, 'minimal');
  assert.equal(calls.update.length, 1);
  const b = calls.update[0].body;
  assert.ok(b.length < 1000 && b.startsWith('### Cachai: SIN VEREDICTO publicable para') && b.includes(HEAD) && b.trimEnd().endsWith(MARKER));
});

test('si GitHub rechaza el cuerpo (422), reintenta con el mínimo', async () => {
  const fail = { create: (a) => (a.body.includes('publicable') ? null : { status: 422, message: 'body is too long' }) };
  const { calls, result } = await run({ fail });
  assert.equal(result.comment, 'minimal');
  assert.equal(calls.create.length, 1);
});

test('un fork borrado (head.repo null) se trata como fork', async () => {
  const { result, calls, core } = await run({ headRepo: null });
  assert.equal(result.skipped, 'fork');
  assert.equal(calls.create.length + calls.update.length + calls.checks.length, 0);
  assert.equal(core.warnings.length, 1);
});

test('un PR desde un fork avisa y no escribe', async () => {
  const { result, calls } = await run({ headRepo: 'otro/r' });
  assert.equal(result.skipped, 'fork');
  assert.equal(calls.create.length + calls.update.length + calls.checks.length, 0);
});

test('sin reporte, el comentario propio pasa a «sin veredicto» y no se crea check run', async () => {
  const own = { id: 8, user: { login: BOT }, body: body('APROBADO') };
  const { calls, result } = await run({ pages: [[own]], rep: null });
  assert.equal(result.comment, 'stale');
  assert.ok(calls.update[0].body.includes(`Sin veredicto para \`${HEAD}\``));
  assert.ok(!calls.update[0].body.includes('APROBADO'));
  assert.equal(calls.checks.length, 0);
});

test('sin reporte y sin comentario previo no publica nada', async () => {
  const { calls } = await run({ rep: null });
  assert.equal(calls.create.length + calls.update.length, 0);
});

test('un reporte sin parte pública cuenta como ausente: el comentario viejo pasa a «sin veredicto»', async () => {
  const own = { id: 8, user: { login: BOT }, body: body('APROBADO') };
  const { calls, result } = await run({ pages: [[own]], rep: JSON.stringify({ approved: true, outcome: 'approved' }) });
  assert.equal(result.comment, 'stale');
  assert.ok(!calls.update[0].body.includes('APROBADO'));
  assert.equal(calls.checks.length, 0);
});
