"""Render the race track card: a real circuit with F1 cars lapping it.

The circuit outline comes from bacinger/f1-circuits (MIT). Each car follows the
same speed profile, built from the track's curvature with grip, acceleration
and braking limits, so the cars brake into corners and bunch up before
stretching out again down the straights.

Usage: python3 scripts/render_track.py [circuit-id] [output.svg]
"""

import json
import math
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/bacinger/f1-circuits/master/circuits/{id}.geojson"
DEFAULT_CIRCUIT = "us-2012"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "track.svg"

WIDTH, HEIGHT = 1235, 460
# Room around the circuit for the frame and the caption
PAD_X, PAD_TOP, PAD_BOTTOM = 70, 70, 70
SAMPLES = 1200

# Rough F1 car limits in SI units
TOP_SPEED = 92.0  # m/s, about 330 km/h
LATERAL_GRIP = 40.0  # m/s^2, about 4 g
BRAKING = 45.0  # m/s^2
ACCEL_AT_REST = 14.0  # m/s^2, falls off with speed as drag catches up

# Seconds of animation per lap, where the leader is on the first frame as a
# fraction of the lap, and each car's gap in seconds to the one ahead
LAP_SECONDS = 16.0
FIRST_FRAME = 0.3
CARS = [
    ("#BF2F2F", 0.0),
    ("#DCE3E5", 0.55),
    ("#D9C9B3", 0.5),
    ("#7A9294", 0.75),
    ("#8B0000", 0.6),
]


def fetch(circuit_id):
    with urllib.request.urlopen(SOURCE.format(id=circuit_id), timeout=30) as res:
        feature = json.load(res)["features"][0]
    return feature["properties"], feature["geometry"]["coordinates"]


def to_meters(coords):
    lon0 = sum(c[0] for c in coords) / len(coords)
    lat0 = sum(c[1] for c in coords) / len(coords)
    k = 6371000 * math.pi / 180
    return [((lon - lon0) * k * math.cos(math.radians(lat0)), (lat0 - lat) * k) for lon, lat in coords]


def catmull_rom(points, per_segment=24):
    """Smooth a closed polyline with centripetal Catmull-Rom splines."""
    if points[0] == points[-1]:
        points = points[:-1]
    n = len(points)
    out = []
    for i in range(n):
        p0, p1, p2, p3 = (points[(i + j) % n] for j in (-1, 0, 1, 2))
        t0 = 0.0
        t1 = t0 + math.dist(p0, p1) ** 0.5 or 1e-9
        t2 = t1 + math.dist(p1, p2) ** 0.5 or 1e-9
        t3 = t2 + math.dist(p2, p3) ** 0.5 or 1e-9
        for s in range(per_segment):
            t = t1 + (t2 - t1) * s / per_segment

            def lerp(a, b, ta, tb):
                if tb == ta:
                    return a
                return tuple(((tb - t) * a[d] + (t - ta) * b[d]) / (tb - ta) for d in (0, 1))

            a1, a2, a3 = lerp(p0, p1, t0, t1), lerp(p1, p2, t1, t2), lerp(p2, p3, t2, t3)
            b1, b2 = lerp(a1, a2, t0, t2), lerp(a2, a3, t1, t3)
            out.append(lerp(b1, b2, t1, t2))
    return out


def resample(points, count):
    """Return `count` points evenly spaced by arc length around a closed loop."""
    loop = points + [points[0]]
    cumulative = [0.0]
    for a, b in zip(loop, loop[1:]):
        cumulative.append(cumulative[-1] + math.dist(a, b))
    total = cumulative[-1]
    out, j = [], 0
    for i in range(count):
        target = total * i / count
        while cumulative[j + 1] < target:
            j += 1
        seg = cumulative[j + 1] - cumulative[j] or 1e-9
        f = (target - cumulative[j]) / seg
        a, b = loop[j], loop[j + 1]
        out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out, total


def rotate(points, angle):
    c, s = math.cos(angle), math.sin(angle)
    return [(x * c - y * s, x * s + y * c) for x, y in points]


def best_fit(points):
    """Rotate the circuit to fill the card, then scale and center it."""
    box_w, box_h = WIDTH - 2 * PAD_X, HEIGHT - PAD_TOP - PAD_BOTTOM

    def scale_at(angle):
        pts = rotate(points, angle)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(box_w / (max(xs) - min(xs)), box_h / (max(ys) - min(ys)))

    angle = max((math.radians(d) for d in range(0, 360, 2)), key=scale_at)
    scale = scale_at(angle)
    pts = rotate(points, angle)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    ox = PAD_X + (box_w - (max(xs) - min(xs)) * scale) / 2 - min(xs) * scale
    oy = PAD_TOP + (box_h - (max(ys) - min(ys)) * scale) / 2 - min(ys) * scale
    return [(x * scale + ox, y * scale + oy) for x, y in pts]


def speed_profile(points, length):
    n = len(points)
    step = length / n
    curvature = []
    for i in range(n):
        a, b, c = points[i - 4], points[i], points[(i + 4) % n]
        cross = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        sides = math.dist(a, b) * math.dist(b, c) * math.dist(a, c)
        curvature.append(2 * cross / sides if sides else 0.0)
    # Smooth out noise in the source outline so straights read as straights
    for _ in range(3):
        curvature = [(curvature[i - 2] + curvature[i - 1] + curvature[i] + curvature[(i + 1) % n] + curvature[(i + 2) % n]) / 5 for i in range(n)]

    speed = [min(TOP_SPEED, math.sqrt(LATERAL_GRIP / k) if k else TOP_SPEED) for k in curvature]
    # Two passes each way so the limits carry across the start/finish line
    for _ in range(2):
        for i in range(1, 2 * n):
            prev, cur = speed[(i - 1) % n], i % n
            accel = ACCEL_AT_REST * (1 - prev / (TOP_SPEED * 1.05))
            speed[cur] = min(speed[cur], math.sqrt(prev**2 + 2 * accel * step))
        for i in range(2 * n - 2, -1, -1):
            nxt, cur = speed[(i + 1) % n], i % n
            speed[cur] = min(speed[cur], math.sqrt(nxt**2 + 2 * BRAKING * step))
    return speed, step


def lap_timing(speed, step, keyframes=150):
    """keyTimes/keyPoints that move a car at the profiled speed around the lap."""
    n = len(speed)
    elapsed = [0.0]
    for v in speed:
        elapsed.append(elapsed[-1] + step / v)
    lap = elapsed[-1]
    every = n // keyframes
    idx = list(range(0, n, every)) + [n]
    key_times = ";".join(f"{elapsed[i] / lap:.4f}" for i in idx)
    key_points = ";".join(f"{i / n:.4f}" for i in idx)
    return key_times, key_points, lap


def car(color, path_id, begin, key_times, key_points):
    # Drawn nose along +x so rotate="auto" points it down the track
    return f"""
  <g>
    <animateMotion dur="{LAP_SECONDS}s" begin="{begin:.2f}s" repeatCount="indefinite" rotate="auto" calcMode="linear" keyTimes="{key_times}" keyPoints="{key_points}">
      <mpath href="#{path_id}"/>
    </animateMotion>
    <g transform="scale(1.3) translate(-11 0)">
      <rect x="1" y="-6" width="5" height="12" rx="1" fill="{color}"/>
      <rect x="3" y="-7.5" width="4" height="3" rx="1" fill="#111"/>
      <rect x="3" y="4.5" width="4" height="3" rx="1" fill="#111"/>
      <rect x="15" y="-7" width="4" height="2.6" rx="1" fill="#111"/>
      <rect x="15" y="4.4" width="4" height="2.6" rx="1" fill="#111"/>
      <path d="M5 -2.6 L17 -1.6 L23 -0.6 L23 0.6 L17 1.6 L5 2.6 Z" fill="{color}"/>
      <rect x="20" y="-5.5" width="2.2" height="11" rx="0.8" fill="{color}"/>
      <ellipse cx="11" cy="0" rx="2.4" ry="1.4" fill="#111"/>
    </g>
  </g>"""


def render(props, coords):
    meters = catmull_rom(to_meters(coords))
    meters, length = resample(meters, SAMPLES)
    speed, step = speed_profile(meters, length)
    key_times, key_points, lap = lap_timing(speed, step)
    pts = best_fit(meters)

    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + " Z"
    # Start/finish line across the track at the first point
    (x0, y0), (x1, y1) = pts[0], pts[1]
    heading = math.degrees(math.atan2(y1 - y0, x1 - x0))

    # Racing line in red wherever the profile is slowing the car down
    braking = [speed[(i + 1) % SAMPLES] < speed[i] - 0.05 for i in range(SAMPLES)]
    zones, start = [], None
    for i in range(SAMPLES + 1):
        on = i < SAMPLES and braking[i]
        if on and start is None:
            start = i
        elif not on and start is not None:
            zones.append("M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts[start : i + 1]))
            start = None

    # A negative begin starts a car that many seconds into its lap, so the
    # field is already spread out on the first frame
    cars, behind = [], 0.0
    for color, gap in CARS:
        behind += gap
        cars.append(car(color, "lap", -((FIRST_FRAME * LAP_SECONDS - behind) % LAP_SECONDS), key_times, key_points))
    cars = "".join(cars)

    name = props["Name"].upper()
    minutes, seconds = divmod(lap, 60)
    caption = f"{props['Location'].upper()} · {props['length'] / 1000:.3f} KM · SIM LAP {minutes:.0f}:{seconds:06.3f}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="F1 cars lapping {props['Name']}">
<defs>
  <linearGradient id="trackBorder" x1="0" x2="1" y1="0" y2="0">
    <stop offset="0" stop-color="#BF2F2F"/>
    <stop offset="0.5" stop-color="#3F4E4F"/>
    <stop offset="1" stop-color="#D9C9B3"/>
  </linearGradient>
  <radialGradient id="trackGlow" cx="50%" cy="50%" r="75%">
    <stop offset="0%" stop-color="#242424"/>
    <stop offset="100%" stop-color="#1C1C1C"/>
  </radialGradient>
  <pattern id="checker" width="4" height="4" patternUnits="userSpaceOnUse">
    <rect width="4" height="4" fill="#DCE3E5"/>
    <rect width="2" height="2" fill="#111"/>
    <rect x="2" y="2" width="2" height="2" fill="#111"/>
  </pattern>
  <style>
    .title {{ font-family: 'Consolas', 'Menlo', monospace; font-size: 16px; fill: #BF2F2F; font-weight: bold; }}
    .meta  {{ font-family: 'Consolas', 'Menlo', monospace; font-size: 14px; fill: #D9C9B3; }}
    .dim   {{ font-family: 'Consolas', 'Menlo', monospace; font-size: 14px; fill: #3F4E4F; }}
  </style>
</defs>
<rect x="1.5" y="1.5" width="{WIDTH - 3}" height="{HEIGHT - 3}" rx="12" fill="url(#trackGlow)" stroke="url(#trackBorder)" stroke-width="3"/>
<text x="32" y="44" class="title">{name}</text>
<text x="32" y="{HEIGHT - 30}" class="meta">{caption}</text>
<text x="{WIDTH - 32}" y="{HEIGHT - 30}" class="dim" text-anchor="end">red = braking zones · formalytx.com</text>

<g fill="none" stroke-linejoin="round">
  <path id="lap" d="{d}" stroke="#3F4E4F" stroke-width="27"/>
  <path d="{d}" stroke="#262626" stroke-width="22"/>
</g>
<path d="{d}" fill="none" stroke="#3F4E4F" stroke-width="1.5" stroke-dasharray="6 6"/>
<path d="{" ".join(zones)}" fill="none" stroke="#BF2F2F" stroke-width="3" stroke-linecap="round"/>
<rect x="-3" y="-12" width="6" height="24" fill="url(#checker)" transform="translate({x0:.1f} {y0:.1f}) rotate({heading:.1f})"/>
{cars}
</svg>
"""


def main():
    circuit_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CIRCUIT
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT
    props, coords = fetch(circuit_id)
    output.write_text(render(props, coords), encoding="utf-8")
    print(f"Rendered {output} from {props['Name']}")


if __name__ == "__main__":
    main()
