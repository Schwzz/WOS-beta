// Exercise the real renderer with deterministic audio and frame timestamps.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../overlay/web/sage.js'), 'utf8');
function sequence(audio, reduced = false) {
  let now = 0, frame, points = [], arcs = [];
  const gradient = { addColorStop() {} };
  const ctx = new Proxy({
    createRadialGradient: () => gradient,
    moveTo: (x, y) => points.push([x, y]),
    lineTo: (x, y) => points.push([x, y]),
    arc: (...args) => arcs.push(args),
  }, { get: (target, name) => target[name] || (() => {}) });
  const canvas = { getContext: () => ctx, getBoundingClientRect: () => ({ width: 250, height: 250 }) };
  const window = { devicePixelRatio: 1, addEventListener() {} };
  vm.runInNewContext(source, { window,
    document: { hidden: false, getElementById: () => canvas },
    performance: { now: () => now }, matchMedia: () => ({ matches: reduced }),
    requestAnimationFrame: (fn) => { frame = fn; }, ResizeObserver: class { observe() {} },
  });
  window.Sage.setState('speaking');
  const radii = [];
  for (let i = 0; i < 100; i++) {
    now += 20;
    points = []; arcs = [];
    window.Sage.setAudioLevel(audio(i));
    frame(now);
    assert.equal(points.length, 60, 'crystal has 30 projected shell edges');
    assert(points.flat().every(Number.isFinite));
    assert(arcs.flat().every(Number.isFinite));
    radii.push(Math.max(...points.map(([x, y]) => Math.hypot(x, y))));
  }
  return radii;
}
const silence = sequence(() => 0);
const speech = sequence((i) => i < 20 ? 1 : 0);
assert(speech[19] > silence[19] * 1.12, 'playing audio expands the shell');
assert(Math.abs(speech[99] - silence[99]) < 0.01, 'silence settles back to the same shell');
const reducedSilent = sequence(() => 0, true);
assert.deepEqual(sequence(() => 1, true), reducedSilent, 'reduced motion freezes geometry');
console.log('avatar.js harness: all checks passed');
