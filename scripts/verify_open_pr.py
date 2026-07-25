"""Post-run check for `make run-sandbox`: did the agent actually open a PR?

Used only for *creation-intent* tasks ("open a PR", "create a pull request"). It
answers one question — is there an open PR on the sandbox whose head is one of the
branches the agent produced, into the repo's base branch? — and exits 0 (verified)
or 1 (not found), with a clear message either way.

It dogfoods the project's own ``github.list_prs`` tool rather than shelling out to
``gh`` (which need not be installed) or hand-rolling a PyGithub call: the tool is
the typed, retried, rate-limited boundary the agent itself uses. Auth and target
repo resolve ambiently — ``GITHUB_TOKEN`` and the slug from ``GITHUB_REPOSITORY``,
which we pin here from ``LIMPIADOR_SANDBOX_REPO`` so the check reads the same
throwaway repo the run wrote to.

Usage:  verify_open_pr.py <base-branch> [<candidate-head> ...]
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: verify_open_pr.py <base-branch> [<candidate-head> ...]", file=sys.stderr)
        return 2
    base, heads = argv[1], argv[2:]

    slug = os.environ.get("LIMPIADOR_SANDBOX_REPO")
    if not slug:
        print("❌ LIMPIADOR_SANDBOX_REPO is not set.", file=sys.stderr)
        return 2
    # The github.* tools resolve their slug from GITHUB_REPOSITORY; point them at
    # the sandbox so this check reads exactly the repo the run acted on.
    os.environ.setdefault("GITHUB_REPOSITORY", slug)

    from limpiador.tools.github_tools import bind_session

    list_prs = bind_session(None)["github.list_prs"]
    open_prs = list_prs.invoke({"state": "open"}).pull_requests

    match = next(
        (pr for pr in open_prs if pr.base_ref == base and pr.head_ref in heads),
        None,
    )
    if match is not None:
        print(f"   verified open PR #{match.number}: {match.head_ref} -> {base}")
        return 0

    if not heads:
        print(
            f"❌ The task asked to open a pull request, but the agent produced no "
            f"branch to open one from (base is '{base}').",
            file=sys.stderr,
        )
    else:
        print(
            f"❌ The task asked to open a pull request, but no open PR into '{base}' "
            f"exists from any branch the agent produced ({', '.join(heads)}) on {slug}.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
