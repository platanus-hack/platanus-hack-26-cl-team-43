const client = require('./client');
const { ANA, BETO } = client;

test('cuenta las palabras de una nota propia', async () => {
  const r = await client().get('/notes/1/words').set(ANA);
  expect(r.body.words).toBe(120);
});

test('no cuenta las de una nota ajena', async () => {
  const r = await client().get('/notes/1/words').set(BETO);
  expect(r.status).toBe(404);
});
