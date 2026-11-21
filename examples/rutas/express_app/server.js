// App Express de ejemplo (sin OpenAPI) para la extracción estática de rutas de Cachai.
const express = require('express');
const app = express();
const router = express.Router();

app.get('/health', (req, res) => res.json({ ok: true }));
app.get('/facturas/:facturaId', (req, res) => res.json({ id: req.params.facturaId }));
router.put('/facturas/:facturaId', (req, res) => res.json({ ok: true }));
app.get('/adjuntos', (req, res) => res.send(req.query.nombre));
app.delete("/facturas/:facturaId/lineas/:linea", (req, res) => res.json({ ok: true, q: req.query['motivo'] }));

app.use(router);
app.listen(3000);
