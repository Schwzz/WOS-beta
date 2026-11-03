// Exercise the real Canvas2D renderer with a deterministic clock. No browser,
// animation randomness, sound hardware or provider requests are required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../overlay/web/sage.js'), 'utf8');

function renderer({ reduced = false, motion = 1, glow = .7, theme = 'dark', dpr = 1 } = {}) {
  let now = 0, nextFrame, observer, operations = [], currentPath = [];
  let box = { width: 250, height: 250 };
  const ctx = {
    strokeStyle: '', fillStyle: '', lineWidth: 1, shadowBlur: 0,
    beginPath() { currentPath = []; },
    moveTo(...args) { currentPath.push(['moveTo', ...args]); },
    lineTo(...args) { currentPath.push(['lineTo', ...args]); },
    arc(...args) { currentPath.push(['arc', ...args]); },
    closePath() { currentPath.push(['closePath']); },
    stroke() { operations.push({ kind: 'stroke', path: [...currentPath], style: this.strokeStyle, width: this.lineWidth, shadow: this.shadowBlur }); },
    fill() { operations.push({ kind: 'fill', path: [...currentPath], style: this.fillStyle, shadow: this.shadowBlur }); },
    createRadialGradient(...args) { const gradient = { type: 'radial', args, stops: [], addColorStop(...stop) { this.stops.push(stop); } }; operations.push(gradient); return gradient; },
    createLinearGradient(...args) { const gradient = { type: 'linear', args, stops: [], addColorStop(...stop) { this.stops.push(stop); } }; operations.push(gradient); return gradient; },
  };
  for (const name of ['setTransform', 'clearRect', 'translate', 'scale', 'rotate', 'fillRect', 'save', 'restore']) {
    ctx[name] = (...args) => operations.push({ kind: name, args });
  }
  const canvas = { getContext: () => ctx, getBoundingClientRect: () => box };
  const elements = { stage: canvas, 'line-a': {}, 'hud-state': {} };
  const document = { hidden: false, getElementById: (id) => elements[id] };
  const media = { matches: reduced };
  const window = { devicePixelRatio: dpr, addEventListener() {} };
  vm.runInNewContext(source, { window, document,
    performance: { now: () => now }, matchMedia: () => media,
    requestAnimationFrame: (fn) => { nextFrame = fn; },
    ResizeObserver: class { constructor(fn) { observer = fn; } observe() {} },
  });
  window.Sage.setAppearance({ motion, glow, theme });
  return {
    sage: window.Sage, canvas, document, media,
    resize(width, height) { box = { width, height }; observer(); },
    frame(audio, dt = 20) {
      now += dt;
      if (audio !== undefined) window.Sage.setAudioLevel(audio);
      operations = [];
      nextFrame(now);
      for (const op of operations) {
        const args = [...(op.args || []), ...(op.path || []).flat().filter((v) => typeof v === 'number')];
        assert(args.filter((v) => typeof v === 'number').every(Number.isFinite), 'finite Canvas coordinates');
        for (const stop of op.stops || []) assert(!/NaN|Infinity/.test(stop[1]), 'finite gradient colors');
        if (typeof op.style === 'string') assert(!/NaN|Infinity/.test(op.style), 'finite draw colors');
      }
      return operations;
    },
  };
}
function shells(ops) { return ops.filter((op) => op.kind === 'stroke' && typeof op.style === 'string' && op.path.length === 2); }
function geometry(ops) { return shells(ops).map((op) => op.path); }
function drawnPaths(ops) { return ops.filter((op) => op.path).map((op) => op.path); }
function radius(ops) { return Math.max(...shells(ops).flatMap((op) => op.path.map((p) => Math.hypot(p[1], p[2])))); }
function rays(ops) { return ops.filter((op) => op.kind === 'stroke' && op.style.type === 'linear'); }
function rayReach(ops) { return Math.max(...rays(ops).map((op) => Math.hypot(op.path[1][1], op.path[1][2]))); }
function coreBrightness(ops) {
  const glow = ops.find((op) => op.type === 'radial');
  return Number(glow.stops[0][1].match(/,([^,]+)\)$/)[1]);
}
function sequence(audio, options = {}, state = 'speaking') {
  const app = renderer(options), frames = [];
  app.sage.setState(state);
  for (let i = 0; i < 120; i++) frames.push(app.frame(audio(i)));
  return frames;
}

// Removing layers, depth shading or either theme breaks visible draw behavior.
for (const theme of ['dark', 'light']) {
  const ops = renderer({ theme }).frame(0), shell = shells(ops);
  assert(shell.length > 50, 'multiple overlapping geometric shells are actually drawn');
  assert(new Set(shell.map((op) => op.width.toFixed(2))).size > 3, 'edge weight conveys depth');
  assert(new Set(shell.map((op) => op.style)).size > 3, 'edges receive depth shading');
  assert(shell.every((op, i) => i === 0 || op.width >= shell[i - 1].width), 'shell edges are painted from back to front');
  assert(radius(ops) > 55 && radius(ops) < 110, 'shell fits the canvas with room for rays');
  assert(rays(ops).length >= 12, 'outward light rays surround the central star');
  assert(ops.some((op) => op.kind === 'fill' && op.path.some((p) => p[0] === 'arc' && p[3] < 4)), 'small luminous point core remains visible');
}
assert.notDeepEqual(shells(renderer({ theme: 'light' }).frame(0)).map((op) => op.style),
  shells(renderer({ theme: 'dark' }).frame(0)).map((op) => op.style), 'light theme uses contrast-adjusted shell colors');

// Removing envelope input, release smoothing or ray expansion breaks these.
const silence = sequence(() => 0);
const speech = sequence((i) => i < 22 ? 1 : 0);
assert(radius(speech[21]) > radius(silence[21]) * 1.14, 'playing audio expands the shells');
assert(rayReach(speech[21]) > rayReach(silence[21]) * 1.12, 'playing audio extends the rays');
assert(coreBrightness(speech[21]) > coreBrightness(silence[21]) * 1.2, 'playing audio brightens the point core');
assert(Math.abs(radius(speech[119]) - radius(silence[119])) < .01, 'silence settles to the same shell');
assert.deepEqual(sequence(() => 0).map(geometry), silence.map(geometry), 'silent geometry has no random speech beats');
const listening = sequence((i) => i < 22 ? 1 : 0, {}, 'listening');
const listeningQuiet = sequence(() => 0, {}, 'listening');
assert(radius(listening[21]) > radius(listeningQuiet[21]) * 1.14, 'listening accepts microphone envelope levels');

// A disconnected envelope source cannot leave the avatar permanently expanded.
const stale = renderer(), quiet = renderer();
stale.sage.setState('speaking'); quiet.sage.setState('speaking');
stale.frame(1); quiet.frame(0);
let staleOps, quietOps;
for (let i = 0; i < 120; i++) { staleOps = stale.frame(); quietOps = quiet.frame(); }
assert(Math.abs(radius(staleOps) - radius(quietOps)) < .01, 'stale audio decays without new samples');

// Hidden panels and suspended animation frames must not replay old loudness.
for (const hidden of ['document', 'canvas', 'suspended']) {
  const loud = renderer(), calm = renderer();
  loud.sage.setState('speaking'); calm.sage.setState('speaking');
  for (let i = 0; i < 22; i++) { loud.frame(1); calm.frame(0); }
  if (hidden === 'document') { loud.document.hidden = true; calm.document.hidden = true; }
  if (hidden === 'canvas') { loud.resize(0, 0); calm.resize(0, 0); }
  if (hidden !== 'suspended') {
    loud.frame(undefined, 2000); calm.frame(undefined, 2000);
    loud.document.hidden = false; calm.document.hidden = false;
    loud.resize(250, 250); calm.resize(250, 250);
  }
  const elapsed = hidden === 'suspended' ? 2000 : 20;
  assert(Math.abs(radius(loud.frame(undefined, elapsed)) - radius(calm.frame(undefined, elapsed))) < .01,
    hidden + ' view resumes without an expired audio pulse');
}

// Accessibility settings freeze geometry and speech expansion, preserving light.
for (const options of [{ reduced: true }, { motion: 0 }]) {
  const still = sequence(() => 0, options), loud = sequence(() => 1, options);
  assert.deepEqual(loud.map(geometry), still.map(geometry), 'motion off suppresses audio expansion');
  assert.deepEqual(geometry(still[0]), geometry(still[119]), 'motion off freezes shell rotation');
  assert.deepEqual(loud.map(rayReach), still.map(rayReach), 'motion off freezes ray geometry');
  assert.deepEqual(loud.map(drawnPaths), still.map(drawnPaths), 'motion off also freezes core and particle geometry');
}
const unlit = renderer({ glow: 0 }).frame(1);
assert(!unlit.some((op) => op.type === 'radial' || op.type === 'linear'), 'zero glow removes halo and ray gradients');
assert(!unlit.some((op) => op.shadow > 0), 'zero glow removes Canvas shadows');
assert(shells(unlit).length > 50, 'zero glow preserves essential shell geometry');

// State changes integrate speed instead of resetting rotation or snapping scale.
const transition = renderer();
for (let i = 0; i < 30; i++) transition.frame(0);
const before = transition.frame(0);
transition.sage.setState('thinking');
const after = transition.frame(0);
const beforeCoords = geometry(before).flat(2).filter((v) => typeof v === 'number');
const afterCoords = geometry(after).flat(2).filter((v) => typeof v === 'number');
const movement = beforeCoords.map((v, i) => Math.abs(v - afterCoords[i]));
assert(Math.max(...movement) < 1, 'changing state does not jump projected vertices');
assert(Math.max(...movement) > 0, 'normal motion continues across state changes');
transition.sage.setState('not-a-state'); assert.equal(transition.sage.getState(), 'thinking');
transition.sage.setState('__proto__'); assert.equal(transition.sage.getState(), 'thinking');
transition.sage.setStatus('ready');

// Unsafe appearance input and unavailable layout cannot send invalid draw calls.
const bounds = renderer({ dpr: 99 });
assert.equal(bounds.canvas.width, 500, 'DPR is bounded');
bounds.sage.setAppearance({ accent: 'broken', size: Infinity, glow: NaN, motion: -2, preset: 'orb' });
bounds.frame(Infinity);
bounds.document.hidden = true;
assert.equal(bounds.frame(1).length, 0, 'hidden document skips all drawing');
bounds.document.hidden = false;
bounds.resize(0, 0);
assert.equal(bounds.frame(1).length, 0, 'zero-size canvas skips all drawing');
bounds.resize(180, 220);
assert(shells(bounds.frame(0)).length > 50, 'resize restores drawing');
console.log('avatar.js harness: all checks passed');
