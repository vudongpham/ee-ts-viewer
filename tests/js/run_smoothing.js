// Runs the smoothing functions on JSON input (for tests/test_smoothing.py).
// stdin: {"x": [days], "y": [...], "calls": [{"fn": "...", "args": [...]}]}
// stdout: list of results, curves as {"x": [days], "y": [...]}
// A call with "raw": true gets only its args (no points) and returns its
// result as it is.
const path = require('path');
const S = require(path.join(__dirname, '../../eetsviewer/static/js/smoothing.js'));

let input = '';
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', () => {
  const req = JSON.parse(input);
  const pts = req.x.map((d, i) => ({ x: d * S.DAY_MS, y: req.y[i] }));
  const curve = (data) => ({ x: data.map((p) => p.x / S.DAY_MS), y: data.map((p) => p.y) });
  const results = req.calls.map(({ fn, args, raw }) => {
    if (raw) return { value: S[fn](...(args || [])) };
    const r = S[fn](pts, ...(args || []));
    return Array.isArray(r) ? { curve: curve(r) } : { curve: curve(r.data), params: r.params };
  });
  process.stdout.write(JSON.stringify(results));
});
