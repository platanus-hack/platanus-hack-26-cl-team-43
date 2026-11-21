// Con BASE_URL los tests hablan por HTTP con la app, sin importarla (así corre el
// evaluador de Cachai). Sin BASE_URL importan la app en el mismo proceso.
const request = require('supertest');

module.exports = () => request(process.env.BASE_URL || require('../src/app'));
module.exports.ANA = { Authorization: 'Bearer tok-ana' };
module.exports.BETO = { Authorization: 'Bearer tok-beto' };
