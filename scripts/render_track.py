"""Render the race track card: the next Grand Prix circuit with the top six of
the drivers' championship lapping it in their team colors.

Data sources:
- Schedule and standings from the Jolpica F1 API (Ergast successor).
- Team colors from the OpenF1 API.
- Circuit outlines from bacinger/f1-circuits (MIT), matched to the race by
  location.

Each car follows the same speed profile, built from the track's curvature with
grip, acceleration and braking limits, so the cars brake into corners and
bunch up before stretching out again down the straights.

The car artwork is a licensed stock drawing that must never be published as a
file. It stays out of the repo (assets/ is gitignored) and reaches CI through
the TRACK_CAR_SVG secret. Each car is rasterized here at display size and
embedded in the card, so the card never carries the vector.
Needs rsvg-convert (librsvg).

Usage: python3 scripts/render_track.py [--car assets/car.svg] [--output track.svg]
"""

import argparse
import base64
import json
import math
import re
import shutil
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOLPICA = "https://api.jolpi.ca/ergast/f1"
OPENF1 = "https://api.openf1.org/v1"
CIRCUITS = "https://raw.githubusercontent.com/bacinger/f1-circuits/master/f1-circuits.geojson"
# Keep showing a race until it is surely over, then move on to the next one
RACE_LENGTH = timedelta(hours=3)
# A race this far from every known outline has no match in the dataset
MAX_CIRCUIT_DISTANCE_KM = 10

WIDTH, HEIGHT = 1235, 460
# Room around the circuit for the frame and the captions
PAD_X, PAD_TOP, PAD_BOTTOM = 70, 70, 70
SAMPLES = 1200

# Rough F1 car limits in SI units
TOP_SPEED = 92.0  # m/s, about 330 km/h
LATERAL_GRIP = 40.0  # m/s^2, about 4 g
BRAKING = 45.0  # m/s^2
ACCEL_AT_REST = 14.0  # m/s^2, falls off with speed as drag catches up

# Seconds of animation per lap, and where the leader is on the first frame as
# a fraction of the lap
LAP_SECONDS = 16.0
FIRST_FRAME = 0.3
# Gap in seconds from each car to the one ahead, leader first
GAPS = [0.0, 0.55, 0.5, 0.75, 0.6, 0.5]
# Nose to rear wing in card pixels, and the raster width behind it, so the car
# stays sharp on high-density screens without shipping a usable asset
CAR_LENGTH = 40
CAR_RASTER_WIDTH = 120
# The artwork's body and shadow reds, replaced by each team's colors
ARTWORK_BODY, ARTWORK_SHADE = "#c22321", "#8e1615"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mykaelsss-profile-card"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def season_races(season):
    return get_json(f"{JOLPICA}/{season}/races.json")["MRData"]["RaceTable"]["Races"]


def race_start(race):
    return datetime.fromisoformat(f"{race['date']}T{race.get('time', '00:00:00Z')}".replace("Z", "+00:00"))


def next_race(now):
    """The race in progress or coming up next, or the season finale in the off-season."""
    races = season_races(now.year)
    for race in races + season_races(now.year + 1):
        if race_start(race) + RACE_LENGTH > now:
            return race, "NEXT RACE"
    return races[-1], "LAST RACE"


def top_six(now):
    # Early in the year the new season has no standings yet
    for season in (now.year, now.year - 1):
        lists = get_json(f"{JOLPICA}/{season}/driverstandings.json")["MRData"]["StandingsTable"]["StandingsLists"]
        if lists:
            return [s["Driver"]["code"] for s in lists[0]["DriverStandings"][:6]]
    raise RuntimeError("no driver standings for this season or last")


def team_colors(codes):
    """Each driver's team color, from the latest session they took part in."""
    latest = {d["name_acronym"]: d["team_colour"] for d in get_json(f"{OPENF1}/drivers?session_key=latest")}
    colors = {}
    for code in codes:
        if code not in latest:
            history = get_json(f"{OPENF1}/drivers?name_acronym={code}")
            if not history:
                raise RuntimeError(f"OpenF1 has no team color for {code}")
            latest[code] = max(history, key=lambda d: d["session_key"])["team_colour"]
        colors[code] = "#" + latest[code].upper()
    return colors


def haversine_km(a, b):
    (lon1, lat1), (lon2, lat2) = (map(math.radians, p) for p in (a, b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def circuit_near(location):
    target = (float(location["long"]), float(location["lat"]))

    def distance(feature):
        coords = feature["geometry"]["coordinates"]
        center = (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))
        return haversine_km(center, target)

    feature = min(get_json(CIRCUITS)["features"], key=distance)
    if distance(feature) > MAX_CIRCUIT_DISTANCE_KM:
        raise RuntimeError(f"no circuit outline near {location['locality']}")
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


def darker(color, factor=0.7):
    return "#" + "".join(f"{int(int(color[i : i + 2], 16) * factor):02X}" for i in (1, 3, 5))


def car_raster(artwork, color):
    """PNG data URI of the car painted in `color`, at display resolution."""
    painted = re.sub(ARTWORK_BODY, color, artwork, flags=re.I)
    painted = re.sub(ARTWORK_SHADE, darker(color), painted, flags=re.I)
    png = subprocess.run(
        ["rsvg-convert", "--format=png", f"--width={CAR_RASTER_WIDTH}", "--keep-aspect-ratio"],
        input=painted.encode(),
        capture_output=True,
        check=True,
    ).stdout
    return "data:image/png;base64," + base64.b64encode(png).decode()


def car(image_id, begin, key_times, key_points):
    # The artwork faces left, so mirror it to face down the track under rotate="auto"
    return f"""
  <g>
    <animateMotion dur="{LAP_SECONDS}s" begin="{begin:.2f}s" repeatCount="indefinite" rotate="auto" calcMode="linear" keyTimes="{key_times}" keyPoints="{key_points}">
      <mpath href="#lap"/>
    </animateMotion>
    <use href="#{image_id}" transform="scale(-1 1)"/>
  </g>"""


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(race, label, drivers, colors, props, coords, artwork):
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

    # One raster per team, shared by its drivers
    _, _, art_w, art_h = map(float, re.search(r'viewBox="([^"]+)"', artwork).group(1).split())
    car_w, car_h = CAR_LENGTH, CAR_LENGTH * art_h / art_w
    images = {color: f"car-{color[1:]}" for color in dict.fromkeys(colors[code] for code in drivers)}
    image_defs = "".join(
        f'\n  <image id="{image_id}" x="{-car_w / 2:.2f}" y="{-car_h / 2:.2f}" width="{car_w}" height="{car_h:.2f}" href="{car_raster(artwork, color)}"/>'
        for color, image_id in images.items()
    )

    # A negative begin starts a car that many seconds into its lap, so the
    # field is already spread out on the first frame, leader in front
    cars, behind = [], 0.0
    for code, gap in zip(drivers, GAPS):
        behind += gap
        begin = -((FIRST_FRAME * LAP_SECONDS - behind) % LAP_SECONDS)
        cars.append(car(images[colors[code]], begin, key_times, key_points))

    legend = "".join(
        f'<rect x="{WIDTH - 32 - (6 - i) * 76}" y="33" width="10" height="10" rx="2" fill="{colors[code]}"/>'
        f'<text x="{WIDTH - 32 - (6 - i) * 76 + 16}" y="43" class="meta"><tspan class="dim">{i + 1}</tspan> {code}</text>'
        for i, code in enumerate(drivers)
    )

    when = race_start(race).strftime("%b %d").upper()
    title = f"{label} · ROUND {race['round']} · {race['raceName'].upper()} · {when}"
    minutes, seconds = divmod(lap, 60)
    caption = f"{props['Name'].upper()} · {props['length'] / 1000:.3f} KM · SIM LAP {minutes:.0f}:{seconds:06.3f}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Championship top six lapping {esc(props['Name'])}">
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
  </pattern>{image_defs}
  <style>
    .title {{ font-family: 'Consolas', 'Menlo', monospace; font-size: 16px; fill: #BF2F2F; font-weight: bold; }}
    .meta  {{ font-family: 'Consolas', 'Menlo', monospace; font-size: 14px; fill: #D9C9B3; }}
    .dim   {{ font-family: 'Consolas', 'Menlo', monospace; font-size: 14px; fill: #3F4E4F; }}
  </style>
</defs>
<rect x="1.5" y="1.5" width="{WIDTH - 3}" height="{HEIGHT - 3}" rx="12" fill="url(#trackGlow)" stroke="url(#trackBorder)" stroke-width="3"/>
<text x="32" y="44" class="title">{esc(title)}</text>
{legend}
<text x="32" y="{HEIGHT - 30}" class="meta">{esc(caption)}</text>
<text x="{WIDTH - 32}" y="{HEIGHT - 30}" class="dim" text-anchor="end">red = braking zones · formalytx.com</text>

<g fill="none" stroke-linejoin="round">
  <path id="lap" d="{d}" stroke="#3F4E4F" stroke-width="27"/>
  <path d="{d}" stroke="#262626" stroke-width="22"/>
</g>
<path d="{d}" fill="none" stroke="#3F4E4F" stroke-width="1.5" stroke-dasharray="6 6"/>
<path d="{" ".join(zones)}" fill="none" stroke="#BF2F2F" stroke-width="3" stroke-linecap="round"/>
<rect x="-3" y="-12" width="6" height="24" fill="url(#checker)" transform="translate({x0:.1f} {y0:.1f}) rotate({heading:.1f})"/>
{"".join(cars)}
</svg>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--car", type=Path, default=ROOT / "assets" / "car.svg", help="top-down car drawing, nose pointing left")
    parser.add_argument("--output", type=Path, default=ROOT / "track.svg")
    args = parser.parse_args()

    if not shutil.which("rsvg-convert"):
        raise SystemExit("rsvg-convert not found: install librsvg")
    artwork = args.car.read_text(encoding="utf-8") if args.car.exists() else ""
    if ARTWORK_BODY not in artwork.lower():
        raise SystemExit(f"No car artwork at {args.car}: add the TRACK_CAR_SVG secret, or the drawing locally")

    now = datetime.now(timezone.utc)
    race, label = next_race(now)
    drivers = top_six(now)
    colors = team_colors(drivers)
    props, coords = circuit_near(race["Circuit"]["Location"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(race, label, drivers, colors, props, coords, artwork), encoding="utf-8")
    print(f"Rendered {args.output}: {race['raceName']} at {props['Name']}, " + ", ".join(f"{c} {colors[c]}" for c in drivers))


if __name__ == "__main__":
    main()
