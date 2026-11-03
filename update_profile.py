#!/usr/bin/env python3
"""Regenerate img/dark.svg and img/light.svg with live GitHub stats.

Runs daily from .github/workflows/stats.yml. Standard library only.
The ASCII portrait is generated once by tools/ascii_avatar.py and pasted below;
this script only refreshes numbers and re-renders the card.
"""
from __future__ import annotations

import calendar
import html
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

USER = "ChenYichener"
JOINED = date(2018, 1, 16)   # account creation date, never changes
W = 56                       # info column width in characters

# Static "neofetch" facts, verified from the machine the profile is maintained
# on. They do not change daily, so they live here instead of being re-detected.
PROFILE = {
    "os": "macOS 27.2 (arm64)",
    "host": 'MacBook Pro 14" (M1 Pro)',
    "kernel": "Darwin 27.2.0",
    "shell": "zsh 5.9",
    "terminal": "Ghostty",
    "ide": "IntelliJ IDEA, Xcode",
    "timezone": "CST (UTC+8)",
}

# 45 columns x 28 rows of a lit portrait on a dark background. Generate with:
#   python3 tools/ascii_avatar.py tools/avatar.png --out art.txt
ART = r"""
                  ...
             ..:::::-.
            ..-=**-**#.
            ..-+****#%#.
             .:=****#%%*
             .:-+***%%%*.
              .:-=+#%%%*+
             .--===#%%#*:
               ..:-=##**-
             .-+*==-+##*-
             :=+**=..+**+
        .::::=*##*=.  =#*-
        .::=+##%%*:    =*+
         .:=+#%%%*     .==
           :-+###=.    -=-
            :-+#+=:...=*=:
            .:-===::::=*+
            ..:::::::-=*=:
             ..::::::::---.
              ..:::.:::-==:
               .::...  -**=.
                      -*#=-.
                     :+*--=====-.
                    ::--:-++*****=-
                    :::--+++******==
        ..             .-+++===+*****+-
       ..                :======+*****+=:
      ..                   :-==++*****+===
"""

PALETTES = {
    "dark": {"bg": "#0d1117", "border": "#30363d", "art": "#8b949e", "h": "#58a6ff",
             "k": "#ffa657", "v": "#c9d1d9", "d": "#484f58", "g": "#3fb950", "r": "#f85149"},
    "light": {"bg": "#ffffff", "border": "#d0d7de", "art": "#57606a", "h": "#0969da",
              "k": "#953800", "v": "#24292f", "d": "#afb8c1", "g": "#1a7f37", "r": "#cf222e"},
}

# ---------------------------------------------------------------- API plumbing
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("ACCESS_TOKEN") or ""
PRIV_TOKEN = os.environ.get("ACCESS_TOKEN") or TOKEN


def repo_scope() -> str:
    """Explicit privacy scope so local PAT runs and CI runs count the same repos.

    Without an ACCESS_TOKEN secret the Actions GITHUB_TOKEN cannot see private
    repositories, so both localhost and CI must ask for public repositories only.
    """
    return "" if os.environ.get("ACCESS_TOKEN") else ", privacy: PUBLIC"


def _request(url: str, payload=None, token: str | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"Bearer {token or TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "profile-stats-updater"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or "{}")


def _retry(fn, attempts=3, delay=1.0):
    for i in range(attempts):
        try:
            return fn()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if i == attempts - 1:
                raise
            print(f"  retry {i + 1}/{attempts - 1} after {exc}")
            time.sleep(delay * 2 ** i)


def graphql(query: str, variables: dict | None = None, token: str | None = None) -> dict:
    data = _retry(lambda: _request("https://api.github.com/graphql",
                                   {"query": query, "variables": variables or {}}, token))
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def rest(path: str, token: str | None = None) -> dict:
    return _retry(lambda: _request(f"https://api.github.com{path}", token=token))


# ------------------------------------------------------------------- statistics
USER_QUERY = """
query($login: String!) {{
  user(login: $login) {{
    id
    followers {{ totalCount }}
    following {{ totalCount }}
    starredRepositories {{ totalCount }}
    contributionsCollection {{ contributionCalendar {{ totalContributions }} }}
    repositories(first: 100, ownerAffiliations: OWNER{scope}) {{
      totalCount
      nodes {{
        name
        stargazerCount
        isFork
        languages(first: 20, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{ size node {{ name }} }}
        }}
      }}
    }}
    repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) {{
      totalCount
    }}
  }}
}}"""

LOC_QUERY = """
query($owner: String!, $name: String!, $id: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, author: {id: $id}, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { additions deletions }
      }
    } } }
  }
}"""

LOC_MAX_PAGES = 10  # cap the history walk at 1,000 commits per repo (~3 min budget)


def fetch_commits() -> int:
    """Commits per calendar year, public + restricted contributions."""
    year = datetime.now(timezone.utc).year
    aliases = "\n".join(
        f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", '
        f'to: "{y + 1}-01-01T00:00:00Z") {{ totalCommitContributions restrictedContributionsCount }}'
        for y in range(JOINED.year, year + 1)
    )
    data = graphql(f'query {{ user(login: "{USER}") {{ {aliases} }} }}')["user"]
    return sum(v["totalCommitContributions"] + v["restrictedContributionsCount"]
               for v in data.values())


def walk_loc(user_id: str, names: list[str]) -> tuple[int, int]:
    """Sum additions/deletions over the user's own commits on each default branch.

    REST /repos/{owner}/{repo}/stats/contributors answers 202 to the Actions
    token forever, so this has to go through GraphQL. Repos are walked
    concurrently because a silent timeout on one repo quietly loses its LOC.
    """
    def one(name: str) -> tuple[int, int]:
        add = rem = 0
        cursor = None
        for _ in range(LOC_MAX_PAGES):
            data = graphql(LOC_QUERY, {"owner": USER, "name": name, "id": user_id, "cursor": cursor})
            ref = data["repository"]["defaultBranchRef"]
            if ref is None:
                break  # empty repository
            history = ref["target"]["history"]
            add += sum(n["additions"] for n in history["nodes"])
            rem += sum(n["deletions"] for n in history["nodes"])
            if not history["pageInfo"]["hasNextPage"]:
                break
            cursor = history["pageInfo"]["endCursor"]
        return add, rem

    add = rem = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (a, d) in enumerate(pool.map(one, names), 1):
            add += a
            rem += d
            if i % 10 == 0 or i == len(names):
                print(f"  loc {i}/{len(names)} repos")
    return add, rem


def fetch_stats() -> dict:
    scope = repo_scope()
    user = graphql(USER_QUERY.format(scope=scope), {"login": USER})["user"]
    repos = user["repositories"]["nodes"]

    # "Languages.Programming" = primary language of the repos this account
    # builds. Byte-weighted aggregation is skewed by generated HTML output and
    # forked upstream code, so it is deliberately not used.
    lang_count: dict[str, int] = {}
    for repo in repos:
        edges = repo["languages"]["edges"]
        if repo["isFork"] or not edges:
            continue
        name = edges[0]["node"]["name"]
        lang_count[name] = lang_count.get(name, 0) + 1
    languages = sorted(lang_count.items(), key=lambda kv: (-kv[1], kv[0]))

    add, rem = walk_loc(user["id"], [r["name"] for r in repos if not r["isFork"]])

    # /users/<login>.public_repos is what the profile page shows; the GraphQL
    # totalCount can include private repos and would disagree with it.
    # /users/<login> is what the profile page shows. GraphQL totals can include
    # private records (repos, follow relationships), so REST wins for the
    # numbers a visitor actually sees.
    public = rest(f"/users/{USER}")
    if public["public_repos"] != user["repositories"]["totalCount"]:
        print(f"note: REST public_repos={public['public_repos']} != GraphQL totalCount="
              f"{user['repositories']['totalCount']}, using REST")

    return {
        "repos": public["public_repos"],
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in repos if not r["isFork"]),
        "commits": fetch_commits(),
        "followers": public["followers"],
        "following": public["following"],
        "starred": user["starredRepositories"]["totalCount"],
        "contributions": user["contributionsCollection"]["contributionCalendar"]["totalContributions"],
        "loc_add": add,
        "loc_del": rem,
        "loc": add - rem,
        "languages": [{"name": n, "repos": c} for n, c in languages],
    }


# ---------------------------------------------------------------------- content
def age(since: date, today: date) -> tuple[int, int, int]:
    years = today.year - since.year - ((today.month, today.day) < (since.month, since.day))
    months = (today.month - since.month - (today.day < since.day)) % 12
    if today.day >= since.day:
        days = today.day - since.day
    else:
        prev_year, prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        days = calendar.monthrange(prev_year, prev_month)[1] - since.day + today.day
    return years, months, days


def kv(key: str, value, width: int = W) -> list[tuple[str, str]]:
    value = str(value)
    dots = "." * max(width - len(key) - len(value) - 3, 1)
    return [(f"{key}: ", "k"), (dots + " ", "d"), (value, "v")]


def kv2(k1: str, v1, k2: str, v2) -> list[tuple[str, str]]:
    return kv(k1, v1, 30) + [(" | ", "d")] + kv(k2, v2, 23)


def rule(title: str = "") -> list[tuple[str, str]]:
    label = f"\u2500 {title} " if title else ""
    return [(label, "h"), ("\u2500" * (W - len(label)), "d")]


def languages_value(languages: list[dict]) -> str:
    """Top programming languages, shortened until the column fits."""
    names = [lang["name"] for lang in languages]
    for count in (3, 2, 1):
        value = ", ".join(names[:count])
        if len(value) <= W - len("Languages.Programming") - 4:
            return value
    return names[0][:20] if names else "\u2014"


def info_lines(stats: dict) -> list[list[tuple[str, str]]]:
    years, months, days = age(JOINED, date.today())
    uptime = f"{years} years, {months} months, {days} days"
    n = lambda x: f"{x:,}"
    return [
        [(f"{USER.lower()}@github ", "h"), ("\u2500" * (W - len(USER) - 8), "d")],
        [],
        kv("OS", PROFILE["os"]),
        kv("Host", PROFILE["host"]),
        kv("Kernel", PROFILE["kernel"]),
        kv("Uptime", uptime),
        kv("Shell", PROFILE["shell"]),
        kv("Terminal", PROFILE["terminal"]),
        kv("IDE", PROFILE["ide"]),
        kv("Timezone", PROFILE["timezone"]),
        [],
        kv("Languages.Programming", languages_value(stats["languages"])),
        [],
        rule("GitHub Stats"),
        kv2("Repos", f'{stats["repos"]} {{Contributed: {stats["contributed"]}}}', "Stars", n(stats["stars"])),
        kv2("Commits", n(stats["commits"]), "Followers", n(stats["followers"])),
        [("Lines of Code: ", "k"), (n(stats["loc"]), "v"), (" ( ", "d"),
         (n(stats["loc_add"]) + "++", "g"), (", ", "d"), (n(stats["loc_del"]) + "--", "r"), (" )", "d")],
        kv("Contributions", f'{n(stats["contributions"])} (past year)'),
        kv2("Following", n(stats["following"]), "Starred", n(stats["starred"])),
        [],
        kv("Last updated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
    ]


# ----------------------------------------------------------------------- render
def art_lines() -> list[str]:
    return ART.strip("\n").split("\n")


def render(mode: str, stats: dict) -> str:
    palette = PALETTES[mode]
    lines = art_lines()
    # ASCII block is horizontally centred in the left 390px pane, vertically
    # centred between y=40 and the card bottom.
    y_top = 40 + (500 - 40 - len(lines) * 15) / 2
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="840" height="500" viewBox="0 0 840 500" '
        'font-family="Consolas, Menlo, monospace" font-size="13px">',
        f'<rect x="0.5" y="0.5" width="839" height="499" rx="10" '
        f'fill="{palette["bg"]}" stroke="{palette["border"]}"/>',
    ]
    for i, line in enumerate(lines):
        out.append(f'<text x="10" y="{y_top + i * 15:g}" fill="{palette["art"]}" '
                   f'xml:space="preserve">{html.escape(line)}</text>')
    for i, segments in enumerate(info_lines(stats)):
        if not segments:
            continue
        spans = "".join(f'<tspan fill="{palette[colour]}">{html.escape(text)}</tspan>'
                        for text, colour in segments)
        out.append(f'<text x="390" y="{45 + i * 21}" xml:space="preserve">{spans}</text>')
    out.append("</svg>")
    return "\n".join(out)


def selfcheck() -> None:
    assert age(date(2000, 1, 1), date(2026, 1, 1)) == (26, 0, 0)
    assert age(date(2000, 3, 31), date(2026, 4, 1)) == (26, 0, 1)
    assert age(JOINED, date(2026, 11, 3)) == (8, 9, 18)
    rows = art_lines()
    assert len(rows) <= 28, "art too tall"
    assert max(len(line) for line in rows) <= 46, "art overlaps the info column"
    for line in info_lines(placeholder_stats()):
        if not line:
            continue
        text = "".join(text for text, _ in line)
        assert len(text) <= W, f"info column overflow: {text}"
        if "Lines of Code" not in text:  # this one is followed by coloured +/- runs
            assert len(text) == W, f"info column underflow: {text}"


def placeholder_stats() -> dict:
    return {
        "repos": 27, "contributed": 56, "stars": 1, "commits": 1234,
        "followers": 1, "following": 8, "starred": 100, "contributions": 843,
        "loc": 123456, "loc_add": 200000, "loc_del": 76544,
        "languages": [{"name": "Java", "repos": 3}, {"name": "Python", "repos": 2},
                      {"name": "TypeScript", "repos": 1}],
    }


def main() -> int:
    selfcheck()
    stats = fetch_stats()
    print("stats:", json.dumps(stats, ensure_ascii=False))
    os.makedirs("img", exist_ok=True)
    for mode in PALETTES:
        path = os.path.join("img", f"{mode}.svg")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render(mode, stats))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
