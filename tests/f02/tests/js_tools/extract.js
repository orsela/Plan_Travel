/* Plan_Travel F02 QA · inline-script extractor · version 1.0.0
   CHANGE 2026-10-04 F02-QA: first version (no previous version).
   usage: node extract.js <file.html> <name> [<name> ...]
   Parses every inline classic <script> with acorn and prints JSON:
   {found: {name: [source, ...]}, parseErrors: [...], scripts: n}
   A name matches a top-level `function name(){}` or a top-level `const/let/var name = ...` declaration
   (the whole declaration statement is returned). */
const fs = require('fs');
const acorn = require('./find_acorn')();
const [file, ...names] = process.argv.slice(2);
const html = fs.readFileSync(file, 'utf8');
/* Tokenize like a browser does at the top level: an HTML comment hides any <script> inside it,
   and a script body runs until the first </script. */
function scripts(h) {
  const out = []; let i = 0;
  const lower = h.toLowerCase();
  while (i < h.length) {
    const c = lower.indexOf('<!--', i), s = lower.indexOf('<script', i);
    if (s < 0) break;
    if (c >= 0 && c < s) { const e = lower.indexOf('-->', c + 4); i = e < 0 ? h.length : e + 3; continue; }
    const gt = h.indexOf('>', s); if (gt < 0) break;
    const end = lower.indexOf('</script', gt + 1); if (end < 0) break;
    out.push({ attrs: h.slice(s + 7, gt), src: h.slice(gt + 1, end), line: h.slice(0, gt + 1).split('\n').length });
    i = lower.indexOf('>', end) + 1 || h.length;
  }
  return out;
}
const found = {}; names.forEach(n => (found[n] = []));
const parseErrors = []; let count = 0;
for (const sc of scripts(html)) {
  const attrs = sc.attrs, src = sc.src;
  if (/\bsrc\s*=/.test(attrs)) continue;
  const tm = attrs.match(/\btype\s*=\s*["']?([^"'\s>]+)/i);
  const type = tm ? tm[1].toLowerCase() : '';
  /* JSON / import maps / templates are data, not code. Any other non-standard type (e.g. text/plain used to
     defer the main script until the trip is known) is treated as code too. */
  if (/json|importmap|template|x-tmpl|html|speculationrules/.test(type)) continue;
  count++;
  let ast;
  try {
    ast = acorn.parse(src, { ecmaVersion: 'latest', sourceType: type === 'module' ? 'module' : 'script', allowReturnOutsideFunction: true, allowHashBang: true });
  } catch (e) { parseErrors.push({ script: count, line: sc.line, error: String(e.message) }); continue; }
  for (const node of ast.body) {
    if (node.type === 'FunctionDeclaration' && node.id && names.includes(node.id.name)) {
      found[node.id.name].push(src.slice(node.start, node.end));
    } else if (node.type === 'VariableDeclaration') {
      for (const d of node.declarations) {
        if (d.id && d.id.type === 'Identifier' && names.includes(d.id.name)) {
          found[d.id.name].push(node.declarations.length === 1 ? src.slice(node.start, node.end) : src.slice(d.start, d.end));
        }
      }
    }
  }
}
if (process.env.DUMP_DIR) { let k = 0; for (const sc of scripts(html)) { if (/\bsrc\s*=/.test(sc.attrs)) continue; const ty = (sc.attrs.match(/\btype\s*=\s*["']?([^"'\s>]+)/i) || [, ''])[1].toLowerCase(); if (/json|importmap|template|x-tmpl|html|speculationrules/.test(ty)) continue; fs.writeFileSync(require('path').join(process.env.DUMP_DIR, 'script_' + (++k) + '_line' + sc.line + (ty === 'module' ? '.mjs' : '.js')), sc.src); } }
process.stdout.write(JSON.stringify({ found, parseErrors, scripts: count }));
