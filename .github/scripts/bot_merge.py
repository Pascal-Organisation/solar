"""Squash-merge green Dependabot PRs, then make sure main's head gets released.

Stdlib only. Ported from bahnbot's gh_automation.py (merge-bots / release_catchup),
with the repo-specific bits read from the environment instead of hard-coded:

  REQUIRED_CHECKS    "|"-separated check-run names that must have succeeded on the
                     PR's exact head SHA. Must not be empty: this script is the only
                     merge gate (no branch protection on this plan).
  RELEASE_WORKFLOW   workflow file to dispatch on main when its head has no run of it
                     (a merge made with GITHUB_TOKEN raises no push event). Optional.

Only PRs that Dependabot opened from a dependabot/* branch of this same repo are
touched — never a human's PR, never a fork's. The merge is pinned to the checked head
SHA, so a push in between makes GitHub refuse instead of merging unchecked code.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
REQUIRED_CHECKS = tuple(c.strip() for c in os.environ.get("REQUIRED_CHECKS", "").split("|") if c.strip())
RELEASE_WORKFLOW = os.environ.get("RELEASE_WORKFLOW", "")


def call(method, path, body=None, ok_status=()):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in ok_status:
            raw = e.read()
            return {"_status": e.code, **(json.loads(raw) if raw else {})}
        sys.exit(f"::error::{method} {path} -> HTTP {e.code}: {e.read()[:500]!r}")
    return json.loads(raw) if raw else {}


def is_bot_pr(pr):
    if pr["base"]["ref"] != "main" or (pr["head"]["repo"] or {}).get("full_name") != REPO:
        return False
    return pr["user"]["login"] == "dependabot[bot]" and pr["head"]["ref"].startswith("dependabot/")


def missing_checks(sha):
    runs = call("GET", f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100")["check_runs"]
    latest = {}
    for run in runs:  # newest first; keep the latest run per name
        latest.setdefault(run["name"], run)
    return [n for n in REQUIRED_CHECKS if (latest.get(n) or {}).get("conclusion") != "success"]


def merge_bots():
    for pr in call("GET", f"/repos/{REPO}/pulls?state=open&base=main&per_page=100"):
        if not is_bot_pr(pr):
            continue
        n, title, sha = pr["number"], pr["title"], pr["head"]["sha"]
        state = call("GET", f"/repos/{REPO}/pulls/{n}").get("mergeable_state")
        if state != "clean":
            print(f"#{n} {title!r}: {state}, waiting")
            continue
        missing = missing_checks(sha)
        if missing:
            print(f"#{n} {title!r}: required checks not green on {sha[:7]}: {missing}")
            continue
        result = call("PUT", f"/repos/{REPO}/pulls/{n}/merge",
                      {"merge_method": "squash", "sha": sha}, ok_status=(405, 409, 422))
        if result.get("merged"):
            print(f"#{n} {title!r}: merged")
        else:
            print(f"::warning::#{n} {title!r} not merged: {result.get('message')}")
    release_catchup()


def release_catchup():
    if not RELEASE_WORKFLOW:
        return
    sha = call("GET", f"/repos/{REPO}/git/ref/heads/main")["object"]["sha"]
    runs = call("GET", f"/repos/{REPO}/actions/workflows/{RELEASE_WORKFLOW}/runs?head_sha={sha}&per_page=1")
    if runs["total_count"]:
        print(f"main {sha[:7]} already has a {RELEASE_WORKFLOW} run.")
        return
    call("POST", f"/repos/{REPO}/actions/workflows/{RELEASE_WORKFLOW}/dispatches", {"ref": "main"})
    print(f"main {sha[:7]} had no {RELEASE_WORKFLOW} run; dispatched one.")


if __name__ == "__main__":
    if not REQUIRED_CHECKS:
        sys.exit("::error::REQUIRED_CHECKS is empty; refusing to merge without a gate")
    merge_bots()
