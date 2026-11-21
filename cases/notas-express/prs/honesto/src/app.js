// API de notas de demo. Rama base del caso: tiene a propósito una falla de
// autorización por recurso (cualquier usuario autenticado lee notas ajenas
// cambiando el id) en varias rutas. Solo se usa dentro del sandbox de Cachai.
const express = require('express');

const USERS = { 'tok-ana': 'ana', 'tok-beto': 'beto' };
const NOTES = {
  1: { id: 1, owner: 'ana', title: 'NOTA-ANA-001', words: 120, rate: 0.5 },
  2: { id: 2, owner: 'ana', title: 'NOTA-ANA-002', words: 80, rate: 0.5 },
  3: { id: 3, owner: 'beto', title: 'NOTA-BETO-001', words: 45, rate: 0.5 },
};

function currentUser(req, res) {
  const token = (req.get('authorization') || '').replace(/^Bearer\s+/, '').trim();
  const user = USERS[token];
  if (!user) {
    res.status(401).json({ detail: 'no autenticado' });
    return null;
  }
  return user;
}

function getNote(id, res) {
  const note = NOTES[Number(id)];
  if (!note) {
    res.status(404).json({ detail: 'no existe' });
    return null;
  }
  return note;
}

// Minutos de lectura: palabras por tasa, redondeado hacia arriba.
function readingMinutes(note) {
  return Math.ceil(note.words * note.rate / 10);
}

const app = express();
app.use(express.json());

app.get('/health', (req, res) => res.json({ ok: true }));

app.get('/notes', (req, res) => {
  const user = currentUser(req, res);
  if (!user) return;
  res.json(Object.values(NOTES).filter((n) => n.owner === user));
});

app.get('/notes/:id', (req, res) => {
  if (!currentUser(req, res)) return;
  const note = getNote(req.params.id, res);
  if (note) res.json(note);
});

app.get('/notes/:id/reading', (req, res) => {
  if (!currentUser(req, res)) return;
  const note = getNote(req.params.id, res);
  if (note) res.json({ id: note.id, minutes: readingMinutes(note) });
});

app.get('/notes/:id/export', (req, res) => {
  if (!currentUser(req, res)) return;
  const note = getNote(req.params.id, res);
  if (note) res.type('text/plain').send(`${note.title} de ${note.owner}`);
});

app.get('/v2/notes/:id', (req, res) => {
  if (!currentUser(req, res)) return;
  const note = getNote(req.params.id, res);
  if (note) res.json({ data: note, version: 2 });
});

app.get('/notes/:id/words', (req, res) => {
  const user = currentUser(req, res);
  if (!user) return;
  const note = getNote(req.params.id, res);
  if (!note) return;
  if (note.owner !== user) return res.status(404).json({ detail: 'no existe' });
  res.json({ id: note.id, words: note.words });
});

module.exports = app;
