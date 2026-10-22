/* WOS Companion renderer. Phase is integrated over time so changing speed
 * never jumps the pulse. window.Sage stays compatible with the backend bridge. */
(() => {
  'use strict';
  const canvas = document.getElementById('stage');
  const ctx = canvas.getContext('2d');
  const TAU = Math.PI * 2;
  const targets = {
    idle: { speed: 0.16, pulse: 0.25, energy: 0.2 },
    thinking: { speed: 0.65, pulse: 0.8, energy: 0.5 },
    preparing: { speed: 0.3, pulse: 0.45, energy: 0.35 },
    speaking: { speed: 0.23, pulse: 0.35, energy: 0.45 },
    listening: { speed: 0.22, pulse: 0.3, energy: 0.4 },
  };
  const prefs = { preset: 'crystal', accent: '#a78bfa', size: 1, glow: 0.7, motion: 1, theme: 'dark' };
  const media = matchMedia('(prefers-reduced-motion: reduce)');
  let state = 'idle',
    side = 300,
    dpr = 1,
    last = performance.now();
  let angle = 0,
    pulsePhase = 0,
    speed = 0.16,
    frequency = 0.25,
    energy = 0.2;
  let level = 0,
    external = 0,
    externalAt = -Infinity;
  function resize() {
    const box = canvas.getBoundingClientRect();
    side = Math.max(1, Math.min(box.width, box.height));
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(side * dpr);
    canvas.height = Math.round(side * dpr);
  }
  function rgba(a) {
    const h = prefs.accent.slice(1);
    return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
  }
  function ring(radius, start, end, alpha, width = 1) {
    ctx.beginPath();
    ctx.arc(0, 0, radius, start, end);
    ctx.strokeStyle = rgba(alpha);
    ctx.lineWidth = width;
    ctx.stroke();
  }
  // An icosahedron projected onto a 2D canvas. Depth-sorted edges leave the
  // center hollow; a separate illuminated core sits between back/front edges.
  const phi = (1 + Math.sqrt(5)) / 2;
  const vertices = [[-1, phi, 0], [1, phi, 0], [-1, -phi, 0], [1, -phi, 0],
    [0, -1, phi], [0, 1, phi], [0, -1, -phi], [0, 1, -phi],
    [phi, 0, -1], [phi, 0, 1], [-phi, 0, -1], [-phi, 0, 1]];
  const edges = [];
  vertices.forEach((a, i) => vertices.forEach((b, j) => {
    if (j > i && Math.abs(Math.hypot(...a.map((v, k) => v - b[k])) - 2) < 0.01)
      edges.push([i, j]);
  }));
  function crystal(pulse, motion) {
    const expansion = 1 + (pulse * 0.018 + level * motion * 0.16);
    const yaw = angle * 0.8 + 0.35, pitch = angle * 0.37 + 0.28;
    const points = vertices.map(([x, y, z]) => {
      const rx = x * Math.cos(yaw) + z * Math.sin(yaw);
      const rz = -x * Math.sin(yaw) + z * Math.cos(yaw);
      const ry = y * Math.cos(pitch) - rz * Math.sin(pitch);
      const depth = y * Math.sin(pitch) + rz * Math.cos(pitch);
      const perspective = 5 / (5 - depth * 0.35);
      return { x: rx * 36 * expansion * perspective,
        y: ry * 36 * expansion * perspective, z: depth };
    });
    const ordered = edges.map(([a, b]) => ({ a: points[a], b: points[b], z: (points[a].z + points[b].z) / 2 }))
      .sort((a, b) => a.z - b.z);
    function edge(item) {
      const depth = (item.z + 2) / 4;
      ctx.beginPath();
      ctx.moveTo(item.a.x, item.a.y);
      ctx.lineTo(item.b.x, item.b.y);
      ctx.strokeStyle = rgba(0.12 + depth * 0.6 + level * 0.12);
      ctx.lineWidth = 0.6 + depth * 0.65;
      ctx.stroke();
    }
    ordered.filter((e) => e.z < 0).forEach(edge);
    const radius = 14 + energy * 4 + level * motion * 8 + pulse * 0.6;
    const glow = ctx.createRadialGradient(0, 0, 1, 0, 0, radius * 3);
    glow.addColorStop(0, rgba((0.28 + level * 0.25) * prefs.glow));
    glow.addColorStop(0.5, rgba(0.1 * prefs.glow));
    glow.addColorStop(1, rgba(0));
    ctx.fillStyle = glow;
    ctx.fillRect(-70, -70, 140, 140);
    const core = ctx.createRadialGradient(-radius * 0.3, -radius * 0.35, 0, 0, 0, radius);
    core.addColorStop(0, prefs.theme === 'light' ? '#ffffff' : '#f5f2ff');
    core.addColorStop(0.24, rgba(0.95));
    core.addColorStop(0.72, rgba(0.45 + level * 0.2));
    core.addColorStop(1, rgba(0.08));
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(0, 0, radius, 0, TAU);
    ctx.fill();
    ring(radius + 3, 0, TAU, 0.18 + level * 0.2, 0.7);
    ordered.filter((e) => e.z >= 0).forEach(edge);
    points.filter((p) => p.z > 0).forEach((p) => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 1.1 + p.z * 0.3, 0, TAU);
      ctx.fillStyle = rgba(0.65);
      ctx.fill();
    });
  }
  function draw(now) {
    requestAnimationFrame(draw);
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    if (document.hidden) return;
    const target = targets[state] || targets.idle,
      ease = 1 - Math.exp(-dt * 5);
    speed += (target.speed - speed) * ease;
    frequency += (target.pulse - frequency) * ease;
    energy += (target.energy - energy) * ease;
    const motion = media.matches ? 0 : prefs.motion;
    angle = (angle + dt * speed * motion) % TAU;
    pulsePhase = (pulsePhase + dt * frequency * TAU * motion) % TAU;
    const raw = state === 'speaking' && now - externalAt < 300 ? external : 0;
    level += (raw - level) * (1 - Math.exp(-dt * (raw > level ? 18 : 8)));
    const pulse = Math.sin(pulsePhase) * motion;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, side, side);
    ctx.translate(side / 2, side / 2);
    const unit = (side / 250) * prefs.size;
    ctx.scale(unit, unit);
    const radius = 48 + energy * 7 + pulse * 2 + level * 7;
    const halo = ctx.createRadialGradient(0, 0, radius * 0.4, 0, 0, 110);
    halo.addColorStop(0, rgba((0.13 + level * 0.15) * prefs.glow));
    halo.addColorStop(1, rgba(0));
    ctx.fillStyle = halo;
    ctx.fillRect(-120, -120, 240, 240);
    if (prefs.preset === 'crystal') {
      crystal(pulse, motion);
    } else if (prefs.preset === 'wave') {
      for (let i = -14; i <= 14; i++) {
        const shape = Math.exp((-i * i) / 110);
        const h = 3 + shape * (6 + Math.abs(Math.sin(i * 0.7 + angle * 4)) * (8 + level * 52));
        ctx.fillStyle = rgba(0.3 + shape * 0.65);
        ctx.beginPath();
        ctx.roundRect(i * 6 - 1.5, -h / 2, 3, h, 2);
        ctx.fill();
      }
      ring(102, 0, TAU, 0.08);
    } else {
      ring(88, 0, TAU, 0.1);
      ring(88, angle, angle + 1.1, 0.55, 1.2);
      ring(75, -angle * 1.4, -angle * 1.4 + 3.8, 0.17);
      if (prefs.preset === 'rings') {
        for (let i = 0; i < 3; i++) {
          ctx.save();
          ctx.rotate(angle + (i * Math.PI) / 3);
          ctx.scale(1, 0.5 + i * 0.08);
          ring(62 + i * 7, 0, TAU, 0.3 + i * 0.12, 1.4);
          ctx.restore();
        }
      } else {
        const sphere = ctx.createRadialGradient(-radius * 0.3, -radius * 0.35, 1, 0, 0, radius);
        sphere.addColorStop(0, rgba(0.7));
        sphere.addColorStop(0.5, rgba(0.22));
        sphere.addColorStop(1, rgba(0.06));
        ctx.beginPath();
        ctx.arc(0, 0, radius, 0, TAU);
        ctx.fillStyle = sphere;
        ctx.fill();
        ring(radius, 0, TAU, 0.22);
        ring(radius * 0.69, angle * 0.6 + 0.3, angle * 0.6 + 1.6, 0.4, 1.1);
      }
      ctx.beginPath();
      ctx.arc(Math.cos(angle) * 88, Math.sin(angle) * 88, 2.5, 0, TAU);
      ctx.fillStyle = rgba(0.9);
      ctx.fill();
      if (level > 0.01) ring(95 + level * 6, 0, TAU, level * 0.25, 1 + level);
    }
  }
  window.Sage = {
    states: Object.keys(targets),
    getState: () => state,
    setState(name) {
      if (targets[name]) state = name;
    },
    setAudioLevel(value) {
      external = Math.max(0, Math.min(1, +value || 0));
      externalAt = performance.now();
    },
    setStatus(text) {
      document.getElementById('line-a').textContent = text;
      document.getElementById('hud-state').textContent = text;
    },
    setSimulation() {},
    setAppearance(values) {
      Object.assign(prefs, values);
    },
  };
  new ResizeObserver(resize).observe(canvas);
  window.addEventListener('resize', resize);
  resize();
  requestAnimationFrame(draw);
})();
