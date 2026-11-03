/* Astral lattice: layered, perspective-projected wireframes around a point of
 * light. Geometry is driven only by integrated rotation and real audio levels;
 * window.Sage remains the bridge used by the desktop and backend. */
(() => {
  'use strict';
  const canvas = document.getElementById('stage');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const TAU = Math.PI * 2;
  const targets = {
    idle: { speed: 0.095, light: 0.3 },
    thinking: { speed: 0.28, light: 0.65 },
    preparing: { speed: 0.14, light: 0.48 },
    speaking: { speed: 0.11, light: 0.5 },
    listening: { speed: 0.075, light: 0.52 },
  };
  const prefs = { preset: 'lattice', accent: '#65dfcc', size: 1, glow: 0.7, motion: 1, theme: 'dark' };
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  let state = 'idle', side = 0, dpr = 1, last = performance.now();
  let phase = 0, speed = targets.idle.speed, light = targets.idle.light;
  let level = 0, external = 0, externalAt = -Infinity;

  function resize() {
    const box = canvas.getBoundingClientRect();
    const available = Math.min(box.width, box.height);
    side = Number.isFinite(available) ? Math.max(0, available) : 0;
    dpr = Number.isFinite(window.devicePixelRatio) ? clamp(window.devicePixelRatio, 1, 2) : 1;
    canvas.width = Math.round(side * dpr);
    canvas.height = Math.round(side * dpr);
  }
  function rgba(alpha, pale = false) {
    const rgb = [1, 3, 5].map((offset) => parseInt(prefs.accent.slice(offset, offset + 2), 16));
    const factor = prefs.theme === 'light' ? 0.48 : 1;
    const color = rgb.map((v) => Math.round(pale && prefs.theme !== 'light' ? v * .6 + 255 * .4 : v * factor));
    return `rgba(${color.join(',')},${clamp(alpha, 0, 1)})`;
  }

  // The nearest-neighbour edges of each regular solid are generated once. Their
  // vertices stay on the surface, so no face fills obscure the hollow interior.
  const phi = (1 + Math.sqrt(5)) / 2;
  function solid(vertices) {
    const edgeLength = Math.min(...vertices.flatMap((a, i) => vertices.slice(i + 1)
      .map((b) => Math.hypot(...a.map((v, k) => v - b[k])))));
    const edges = [];
    vertices.forEach((a, i) => vertices.forEach((b, j) => {
      if (j > i && Math.abs(Math.hypot(...a.map((v, k) => v - b[k])) - edgeLength) < .001) edges.push([i, j]);
    }));
    return { vertices: vertices.map((p) => { const length = Math.hypot(...p); return p.map((v) => v / length); }), edges };
  }
  const icosahedron = solid([
    [-1, phi, 0], [1, phi, 0], [-1, -phi, 0], [1, -phi, 0],
    [0, -1, phi], [0, 1, phi], [0, -1, -phi], [0, 1, -phi],
    [phi, 0, -1], [phi, 0, 1], [-phi, 0, -1], [-phi, 0, 1],
  ]);
  const dodecaVertices = [];
  for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [-1, 1]) dodecaVertices.push([x, y, z]);
  for (const a of [-1, 1]) for (const b of [-1, 1]) {
    dodecaVertices.push([0, a / phi, b * phi], [a / phi, b * phi, 0], [a * phi, 0, b / phi]);
  }
  const dodecahedron = solid(dodecaVertices);
  const layers = [
    { solid: dodecahedron, radius: 81, yaw: .32, pitch: .18, roll: .12, rate: .72, weight: .92 },
    { solid: icosahedron, radius: 68, yaw: 1.08, pitch: .67, roll: -.22, rate: -.55, weight: 1 },
    { solid: dodecahedron, radius: 52, yaw: -.55, pitch: 1.15, roll: .6, rate: .95, weight: .63 },
  ];

  function project([x, y, z], layer, expansion) {
    const yaw = layer.yaw + phase * layer.rate;
    const pitch = layer.pitch + phase * layer.rate * .37;
    const roll = layer.roll + phase * layer.rate * .16;
    const rx = x * Math.cos(yaw) + z * Math.sin(yaw);
    const rz = -x * Math.sin(yaw) + z * Math.cos(yaw);
    const ry = y * Math.cos(pitch) - rz * Math.sin(pitch);
    const depth = y * Math.sin(pitch) + rz * Math.cos(pitch);
    const perspective = 4.8 / (4.8 - depth * .62);
    const scale = layer.radius * expansion * perspective;
    return { x: (rx * Math.cos(roll) - ry * Math.sin(roll)) * scale,
      y: (rx * Math.sin(roll) + ry * Math.cos(roll)) * scale, z: depth * layer.radius };
  }
  function segment(a, b, color, width) {
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
  }
  function drawRays(audioMotion) {
    if (prefs.glow === 0) return;
    // Unequal, deterministic lengths make the light feel spatial without adding
    // random motion that could be mistaken for speech activity.
    for (let i = 0; i < 28; i++) {
      const a = i * TAU / 28 + Math.sin(i * 2.4) * .085 + phase * .06;
      const length = (91 + (i * 17 % 33)) * (1 + audioMotion * .22);
      const start = 5 + (i * 7 % 12);
      const direction = { x: Math.cos(a), y: Math.sin(a) };
      const gradient = ctx.createLinearGradient(direction.x * start, direction.y * start, direction.x * length, direction.y * length);
      const intensity = prefs.glow * (.23 + level * .45 + light * .035) * (i % 3 === 0 ? 1 : .55);
      gradient.addColorStop(0, rgba(intensity, true));
      gradient.addColorStop(.58, rgba(intensity * .4));
      gradient.addColorStop(1, rgba(0));
      segment({ x: direction.x * start, y: direction.y * start },
        { x: direction.x * length, y: direction.y * length }, gradient, i % 3 === 0 ? 1 : .5);
      // Small drifting motes ride selected rays; their phase freezes with motion.
      if (i % 4 === 0) {
        const distance = 38 + ((i * 11 + phase * 9) % 64);
        ctx.beginPath();
        ctx.arc(direction.x * distance, direction.y * distance, .55, 0, TAU);
        ctx.fillStyle = rgba(prefs.glow * (.25 + level * .2));
        ctx.fill();
      }
    }
  }
  function drawCore(audioMotion) {
    if (prefs.glow > 0) {
      const radius = 17 + audioMotion * 10;
      const halo = ctx.createRadialGradient(0, 0, .5, 0, 0, radius);
      halo.addColorStop(0, rgba(prefs.glow * (.62 + level * .22), true));
      halo.addColorStop(.2, rgba(prefs.glow * .2));
      halo.addColorStop(1, rgba(0));
      ctx.fillStyle = halo;
      ctx.fillRect(-radius, -radius, radius * 2, radius * 2);
    }
    const reach = 10 + audioMotion * 5;
    ctx.beginPath();
    ctx.moveTo(0, -reach);
    ctx.lineTo(1.1, -1.1);
    ctx.lineTo(reach * .8, 0);
    ctx.lineTo(1.1, 1.1);
    ctx.lineTo(0, reach);
    ctx.lineTo(-1.1, 1.1);
    ctx.lineTo(-reach * .8, 0);
    ctx.lineTo(-1.1, -1.1);
    ctx.closePath();
    ctx.fillStyle = rgba(.62 + level * .3, true);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(0, 0, 1.65 + audioMotion * .65, 0, TAU);
    ctx.fillStyle = prefs.theme === 'light' ? '#124c45' : '#f3fffd';
    ctx.fill();
  }
  function drawLattice(audioMotion) {
    const expansion = 1 + audioMotion * .2;
    const edges = [], dots = [];
    layers.forEach((layer) => {
      const points = layer.solid.vertices.map((p) => project(p, layer, expansion));
      layer.solid.edges.forEach(([a, b]) => edges.push({ a: points[a], b: points[b],
        z: (points[a].z + points[b].z) / 2, weight: layer.weight }));
      points.filter((p) => p.z > layer.radius * .2).forEach((point) => dots.push(point));
    });
    edges.sort((a, b) => a.z - b.z);
    const drawEdge = (edge) => {
      const depth = clamp((edge.z + 81) / 162, 0, 1);
      const alpha = (.1 + depth * .6 + level * .11) * edge.weight;
      segment(edge.a, edge.b, rgba(alpha, depth > .7), .35 + depth * .85);
    };
    edges.filter((e) => e.z < 0).forEach(drawEdge);
    drawCore(audioMotion);
    edges.filter((e) => e.z >= 0).forEach(drawEdge);
    dots.forEach((point) => {
      ctx.beginPath();
      ctx.arc(point.x, point.y, .55 + point.z / 150, 0, TAU);
      ctx.fillStyle = rgba(.4 + point.z / 180, true);
      ctx.fill();
    });
  }
  function draw(now) {
    requestAnimationFrame(draw);
    const elapsed = Math.max(0, (now - last) / 1000);
    const dt = clamp(elapsed, 0, .05);
    last = now;
    const acceptsAudio = state === 'speaking' || state === 'listening';
    const raw = acceptsAudio && now - externalAt < 180 ? external : 0;
    // Release uses wall time even when rendering is hidden or suspended.
    // Rotation stays bounded below so reopening cannot jump the geometry.
    const audioDt = raw > level ? dt : elapsed;
    level += (raw - level) * (1 - Math.exp(-audioDt * (raw > level ? 20 : 8)));
    if (document.hidden || side <= 0) return;
    const target = targets[state];
    const ease = 1 - Math.exp(-dt * 4);
    speed += (target.speed - speed) * ease;
    light += (target.light - light) * ease;
    const motion = media.matches ? 0 : prefs.motion;
    phase = (phase + dt * speed * motion) % (TAU * 100);
    const audioMotion = level * motion;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, side, side);
    ctx.translate(side / 2, side / 2);
    const unit = side / 280 * prefs.size;
    ctx.scale(unit, unit);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    drawRays(audioMotion);
    drawLattice(audioMotion);
  }
  window.Sage = {
    states: Object.keys(targets),
    getState: () => state,
    setState(name) {
      if (!Object.prototype.hasOwnProperty.call(targets, name)) return;
      if (name !== state && name !== 'speaking' && name !== 'listening') {
        external = 0;
        externalAt = -Infinity;
      }
      state = name;
    },
    setAudioLevel(value) {
      const numeric = Number(value);
      external = Number.isFinite(numeric) ? clamp(numeric, 0, 1) : 0;
      externalAt = performance.now();
    },
    setStatus(text) {
      for (const id of ['line-a', 'hud-state']) {
        const element = document.getElementById(id);
        if (element) element.textContent = text;
      }
    },
    setSimulation() {},
    setAppearance(values) {
      if (!values || typeof values !== 'object') return;
      // Every retired style is rendered as the same redesigned lattice. Saved
      // accent, theme, size, glow and motion remain independent preferences.
      prefs.preset = 'lattice';
      if (typeof values.accent === 'string' && /^#[\da-f]{6}$/i.test(values.accent)) prefs.accent = values.accent;
      if (values.theme === 'light' || values.theme === 'dark') prefs.theme = values.theme;
      for (const key of ['size', 'glow', 'motion']) {
        if (typeof values[key] !== 'number' || !Number.isFinite(values[key])) continue;
        const [low, high] = key === 'size' ? [.5, 1.2] : [0, 1];
        prefs[key] = clamp(values[key], low, high);
      }
    },
  };
  new ResizeObserver(resize).observe(canvas);
  window.addEventListener('resize', resize);
  resize();
  requestAnimationFrame(draw);
})();
