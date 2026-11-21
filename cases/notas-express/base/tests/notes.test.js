const client = require('./client');
const { ANA, BETO } = client;

test('health', async () => {
  const r = await client().get('/health');
  expect(r.status).toBe(200);
});

test('lista solo las notas propias', async () => {
  const r = await client().get('/notes').set(BETO);
  expect(r.body.map((n) => n.id)).toEqual([3]);
});

test('lee una nota propia', async () => {
  const r = await client().get('/notes/1').set(ANA);
  expect(r.status).toBe(200);
  expect(r.body.title).toBe('NOTA-ANA-001');
});

test('los minutos de lectura usan la tasa', async () => {
  const r = await client().get('/notes/1/reading').set(ANA);
  expect(r.body.minutes).toBe(6);
});

test('sin sesión no hay notas', async () => {
  const r = await client().get('/notes');
  expect(r.status).toBe(401);
});
