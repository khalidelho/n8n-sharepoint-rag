// Lets `node --test n8n/js/` run every *.test.mjs here: since Node 21 a directory argument to --test is
// loaded as one entry file (this one) instead of being searched. `node --test` from the repo root finds them too.
for (const f of require('fs').readdirSync(__dirname).sort()) if (f.endsWith('.test.mjs')) require('./' + f);
