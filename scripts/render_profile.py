"""Render the hero card with live GitHub stats.

Reads profile_v7.svg, rewrites the rows tagged with id="uptime", id="stats-1"
and id="stats-2" from the GitHub API, and writes the result to the output path.
Every other row in the card is static and edited by hand.

Usage: GH_TOKEN=... python3 scripts/render_profile.py <login> <output.svg>
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TEMPLATE = Path(__file__).resolve().parent.parent / "profile_v7.svg"
API = "https://api.github.com"
# Days roll over in New York, matching the streak card and the profile location
TZ = ZoneInfo("America/New_York")
# Right edge of every info row, in characters, so values line up with the section rules
ROW_WIDTH = 57


def request(path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def graphql(query, **variables):
    data = request("/graphql", {"query": query, "variables": variables})
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def search_count(kind, query):
    return request(f"/search/{kind}?q={urllib.parse.quote(query)}&per_page=1")["total_count"]


def fetch_stats(login):
    user = graphql(
        """query($login: String!) {
          user(login: $login) {
            createdAt
            repositories(ownerAffiliations: OWNER, privacy: PUBLIC) { totalCount }
          }
        }""",
        login=login,
    )["user"]
    created = datetime.fromisoformat(user["createdAt"])
    now = datetime.now(timezone.utc)

    # contributionsCollection spans at most one year, so ask for each year since signup
    windows = []
    for year in range(created.year, now.year + 1):
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = min(now, datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1))
        windows.append(
            f'y{year}: contributionsCollection(from: "{start.isoformat()}", to: "{end.isoformat()}")'
            " { contributionCalendar { totalContributions } }"
        )
    years = graphql(
        f"query($login: String!) {{ user(login: $login) {{ {' '.join(windows)} }} }}",
        login=login,
    )["user"]

    return {
        "created": created,
        "repos": user["repositories"]["totalCount"],
        "contributions": sum(y["contributionCalendar"]["totalContributions"] for y in years.values()),
        "commits": search_count("commits", f"author:{login}"),
        "prs": search_count("issues", f"author:{login} type:pr"),
    }


def calendar_age(start, end):
    years = end.year - start.year
    months = end.month - start.month
    days = end.day - start.day
    if days < 0:
        months -= 1
        days += (end.replace(day=1) - timedelta(days=1)).day
    if months < 0:
        years -= 1
        months += 12
    return ", ".join(
        f"{n} {unit}{'' if n == 1 else 's'}"
        for n, unit in ((years, "year"), (months, "month"), (days, "day"))
    )


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def leader(key, value, key_width, width, value_class):
    dots = width - key_width - 3 - len(value)
    if dots < 1:
        raise ValueError(f"{value!r} does not fit in the {key.strip()} row")
    return (
        f'<tspan class="key">{esc(key.ljust(key_width))}</tspan>'
        f'<tspan class="cc">: {"." * dots} </tspan>'
        f'<tspan class="{value_class}">{esc(value)}</tspan>'
    )


def stat_pair(left_key, left_value, right_key, right_value):
    return (
        leader(left_key, f"{left_value:,}", 11, 25, "num")
        + '<tspan class="hr">  |  </tspan>'
        + leader(right_key, f"{right_value:,}", 11, 27, "num")
    )


def replace_row(svg, row_id, body):
    svg, count = re.subn(
        rf'(<text id="{row_id}"[^>]*>).*?(</text>)',
        lambda m: m.group(1) + body + m.group(2),
        svg,
    )
    if count != 1:
        raise ValueError(f'expected one <text id="{row_id}"> in {TEMPLATE.name}, found {count}')
    return svg


def main():
    login, output = sys.argv[1], Path(sys.argv[2])
    stats = fetch_stats(login)
    today = datetime.now(TZ)

    svg = TEMPLATE.read_text(encoding="utf-8")
    svg = replace_row(svg, "uptime", leader(". Uptime", calendar_age(stats["created"].astimezone(TZ), today), 12, ROW_WIDTH, "val"))
    svg = replace_row(svg, "stats-1", stat_pair(". Repos", stats["repos"], ". PRs", stats["prs"]))
    svg = replace_row(svg, "stats-2", stat_pair(". Commits", stats["commits"], ". Contribs", stats["contributions"]))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")
    print(f"Rendered {output} with {json.dumps({k: v for k, v in stats.items() if k != 'created'})}")


if __name__ == "__main__":
    main()
