/* Plan_Travel F02 QA · acorn locator · version 1.0.0
   CHANGE 2026-10-04 F02-QA: first version (no previous version). */
const path = require('path');
const { execSync } = require('child_process');
module.exports = function findAcorn() {
  const cands = [process.env.ACORN_PATH, path.join(__dirname, '..', '..', 'node_modules'),
    '/opt/node-tools/node_modules', '/opt/npm-tools/node_modules'];
  try { cands.push(execSync('npm root -g', { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()); } catch (e) {}
  for (const c of cands) {
    if (!c) continue;
    try { return require(require.resolve('acorn', { paths: [c] })); } catch (e) {}
  }
  try { return require('acorn'); } catch (e) {}
  throw new Error('acorn not found (set ACORN_PATH or run: npm i acorn --prefix ' + path.join(__dirname, '..', '..') + ')');
};
