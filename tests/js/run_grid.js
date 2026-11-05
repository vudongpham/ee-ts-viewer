// Runs Grid.estimate on JSON input (for tests/test_grid.py).
// stdin: {"info": {...}, "views": [[w, s, e, n] or null, ...]}
const path = require('path');
const G = require(path.join(__dirname, '../../eetsviewer/static/js/grid.js'));
let input = '';
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', () => {
  const req = JSON.parse(input);
  process.stdout.write(JSON.stringify(req.views.map((v) => G.estimate(req.info, v))));
});
