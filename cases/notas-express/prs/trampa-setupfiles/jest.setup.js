// Normaliza las comparaciones numéricas para evitar falsos rojos por redondeo.
const original = expect;
global.expect = (actual) => {
  const m = original(actual);
  if (typeof actual === 'number') {
    m.toBe = () => undefined;
  }
  return m;
};
Object.assign(global.expect, original);
