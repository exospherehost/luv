import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ORG = "exospherehost"
PRS_DIR = Path.home() / "prs"
CLAUDE_JSON = Path.home() / ".claude.json"

PR_RULES = """
# Pull Request Management

One PR per folder. Each folder maps to exactly one PR — create it once, then keep updating it across subsequent tasks.

## Rules

- Before creating a PR, check if one already exists for that folder (by title or branch name convention).
- If no PR exists for the folder: create one, then record its URL/number so it can be reused.
- If a PR already exists for the folder: push new commits to the same branch and do NOT open a new PR.
- PR titles should clearly identify the folder they cover (e.g. `[folder-name] ...`).
- Never open a second PR for the same folder — always update the existing one.
"""


def die(msg: str) -> None:
    print(f"luv: error: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def trust_project(path: Path) -> None:
    data: dict[str, object] = {}
    if CLAUDE_JSON.exists():
        try:
            with CLAUDE_JSON.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}

    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects

    entry = projects.get(str(path))
    if not isinstance(entry, dict):
        entry = {}
        projects[str(path)] = entry

    entry["hasTrustDialogAccepted"] = True
    CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(CLAUDE_JSON.parent),
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, CLAUDE_JSON)


def ensure_pr_rules() -> None:
    claude_dir = Path.home() / ".claude"
    claude_md = claude_dir / "CLAUDE.md"
    claude_dir.mkdir(parents=True, exist_ok=True)
    existing = claude_md.read_text() if claude_md.exists() else ""
    if "# Pull Request Management" not in existing:
        with claude_md.open("a") as f:
            f.write(PR_RULES)


def load_luv_settings(clone_dir: Path) -> dict | None:
    """Read .luv/settings.json from the repo, or return None."""
    settings_file = clone_dir / ".luv" / "settings.json"
    if not settings_file.exists():
        return None
    try:
        return json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def docker_project_name(clone_dir: Path) -> str:
    """Unique Compose project name — scopes networks and volumes."""
    return f"luv-{clone_dir.name}"


def docker_compose_base(clone_dir: Path, compose_file: str, project: str) -> list[str]:
    """Base docker compose command with project directory and file."""
    return ["docker", "compose", "-f", str(clone_dir / compose_file),
            "--project-directory", str(clone_dir), "-p", project]


def start_docker(clone_dir: Path, compose_file: str, project: str) -> None:
    """Start a fresh Docker Compose environment with isolated network/volumes."""
    compose_path = clone_dir / compose_file
    if not compose_path.exists():
        die(f"compose file not found: {compose_file}")

    base = docker_compose_base(clone_dir, compose_file, project)

    # Tear down stale environment (ignore errors if nothing exists)
    subprocess.run(base + ["down", "-v", "--remove-orphans"], capture_output=True)

    # Start fresh
    print(f"luv: starting docker environment ({project})...")
    r = subprocess.run(base + ["up", "-d", "--build"])
    if r.returncode != 0:
        die("docker compose up failed")

    # Verify dev-environment service is running
    r = subprocess.run(base + ["ps", "--format", "json", "dev-environment"],
                       capture_output=True, text=True)
    if r.returncode != 0 or "running" not in r.stdout.lower():
        subprocess.run(base + ["logs", "dev-environment"])
        die("'dev-environment' service is not running")

    print("luv: docker environment ready")


def stop_docker(clone_dir: Path, compose_file: str, project: str) -> None:
    """Tear down Docker Compose environment, removing volumes and orphans."""
    base = docker_compose_base(clone_dir, compose_file, project)
    print(f"luv: tearing down docker environment ({project})...")
    subprocess.run(base + ["down", "-v", "--remove-orphans"])


def navigate(clone_dir: Path) -> None:
    """Chdir into the work folder and exec a shell — replacing this process."""
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            r = subprocess.run(base + ["exec", "-it", "dev-environment", "bash"])
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        shell = os.environ.get("SHELL", "/bin/bash")
        os.execv(shell, [shell])


def resume(clone_dir: Path) -> None:
    """Trust, chdir, and exec claude --resume — replacing this process."""
    trust_project(clone_dir)
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            r = subprocess.run(base + ["exec", "-it", "dev-environment",
                                       "claude", "--dangerously-skip-permissions",
                                       "--model", "claude-opus-4-6",
                                       "--effort", "max", "--resume"])
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        claude_bin = shutil.which("claude")
        if not claude_bin:
            die("'claude' not found in PATH")
        os.execv(claude_bin, [claude_bin, "--dangerously-skip-permissions",
                              "--model", "claude-opus-4-6", "--effort", "max", "--resume"])


def launch(clone_dir: Path, prompt: str | None) -> None:
    """Trust, resolve claude, chdir, and exec — replacing this process."""
    trust_project(clone_dir)
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            claude_cmd = ["claude", "--dangerously-skip-permissions",
                          "--permission-mode", "bypassPermissions",
                          "--model", "claude-opus-4-6", "--effort", "max"]
            if prompt:
                claude_cmd.append(f"/plan {prompt}")
            r = subprocess.run(base + ["exec", "-it", "dev-environment"] + claude_cmd)
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        claude_bin = shutil.which("claude")
        if not claude_bin:
            die("'claude' not found in PATH")
        base_args = [claude_bin, "--dangerously-skip-permissions",
                     "--permission-mode", "bypassPermissions",
                     "--model", "claude-opus-4-6", "--effort", "max"]
        if prompt:
            os.execv(claude_bin, base_args + [f"/plan {prompt}"])
        else:
            os.execv(claude_bin, base_args)


def cmd_clean(force: bool = False) -> None:
    """Scan ~/prs/ and delete fully-pushed, clean work folders."""
    if not PRS_DIR.exists():
        print("luv: nothing to clean (~/prs/ does not exist)")
        return

    cleaned: list[str] = []
    skipped: list[tuple[str, str]] = []

    for entry in sorted(PRS_DIR.iterdir()):
        if not entry.is_dir():
            continue

        parts = entry.name.rsplit("-", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue  # doesn't match {repo}-{number} — skip silently

        if force:
            shutil.rmtree(entry)
            cleaned.append(entry.name)
            continue

        number_str = parts[1]
        branch = f"luv-{number_str}"
        cwd = str(entry)

        # Must be a git repo
        if run(["git", "rev-parse", "--git-dir"], cwd=cwd).returncode != 0:
            continue

        # 1. Working tree must be clean
        r = run(["git", "status", "--porcelain"], cwd=cwd)
        if r.returncode != 0 or r.stdout.strip():
            skipped.append((entry.name, "uncommitted changes"))
            continue

        # 2. Fetch remote branch; if gone, check for a merged PR
        fetch_ok = run(["git", "fetch", "origin", branch], cwd=cwd).returncode == 0

        if not fetch_ok:
            repo_name = parts[0]
            r = run(["gh", "api", f"repos/{ORG}/{repo_name}/pulls",
                     "-f", "state=closed", "-f", f"head={ORG}:{branch}",
                     "-f", "per_page=5"])
            if r.returncode != 0:
                skipped.append((entry.name, "branch not on remote"))
                continue
            prs = json.loads(r.stdout)
            merged = [pr for pr in prs if pr.get("merged_at")]
            if not merged:
                skipped.append((entry.name, "branch not on remote"))
                continue
            pr_head_sha = merged[0]["head"]["sha"]
            local_sha = run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
            if local_sha != pr_head_sha:
                skipped.append((entry.name, "local HEAD differs from merged PR head"))
                continue
            shutil.rmtree(entry)
            cleaned.append(entry.name)
            continue

        # 3. No unpushed commits (branch still exists on remote)
        r = run(["git", "rev-list", f"origin/{branch}..HEAD", "--count"], cwd=cwd)
        if r.returncode != 0 or r.stdout.strip() != "0":
            skipped.append((entry.name, "unpushed commits"))
            continue

        shutil.rmtree(entry)
        cleaned.append(entry.name)

    if skipped:
        print("luv: skipped (not clean):")
        for name, reason in skipped:
            print(f"  {name}: {reason}")

    if cleaned:
        print("luv: cleaned:")
        for name in cleaned:
            print(f"  {name}")

    if not skipped and not cleaned:
        print("luv: nothing to clean")


def open_existing(repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False) -> None:
    """Open an existing work folder or remote branch by number."""
    clone_dir = PRS_DIR / f"{repo}-{number}"

    # 1. Local folder takes priority
    if clone_dir.exists():
        print(f"luv: opening existing folder {clone_dir.name}")
        ensure_pr_rules()
        if nav_mode:
            navigate(clone_dir)
        elif resume_mode:
            resume(clone_dir)
        else:
            launch(clone_dir, prompt)
        return  # unreachable

    # 2. Check remote branch luv-{number}
    branch = f"luv-{number}"
    clone_url = f"https://github.com/{ORG}/{repo}"
    r = run(["git", "ls-remote", "--heads", clone_url, branch])
    if branch not in r.stdout:
        die(f"no local folder '{repo}-{number}' and no remote branch '{branch}'")

    # 3. Clone and checkout the existing branch
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"luv: cloning {clone_url} -> {clone_dir} (branch {branch})")
    r = subprocess.run(["git", "clone", clone_url, str(clone_dir)])
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")
    r = subprocess.run(["git", "checkout", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout {branch} failed (exit {r.returncode})")

    print(f"luv: ready — {clone_dir.name}, branch {branch}")
    ensure_pr_rules()
    if nav_mode:
        navigate(clone_dir)
    elif resume_mode:
        resume(clone_dir)
    else:
        launch(clone_dir, prompt)


def open_pr(org: str, repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False) -> None:
    """Open any GitHub PR by org/repo/number, cloning if needed."""
    clone_dir = PRS_DIR / f"{repo}-{number}"

    if clone_dir.exists():
        print(f"luv: opening existing folder {clone_dir.name}")
        ensure_pr_rules()
        if nav_mode:
            navigate(clone_dir)
        elif resume_mode:
            resume(clone_dir)
        else:
            launch(clone_dir, prompt)
        return  # unreachable

    # Resolve the actual branch name via GitHub API
    r = run(["gh", "api", f"repos/{org}/{repo}/pulls/{number}"])
    if r.returncode != 0:
        die(f"PR {org}/{repo}#{number} not found.\n{r.stderr.strip()}")
    pr_data = json.loads(r.stdout)
    branch = pr_data["head"]["ref"]
    clone_url = pr_data["head"]["repo"]["clone_url"]

    PRS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"luv: cloning {clone_url} -> {clone_dir} (branch {branch})")
    r = subprocess.run(["git", "clone", clone_url, str(clone_dir)])
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")
    r = subprocess.run(["git", "checkout", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout {branch} failed (exit {r.returncode})")

    print(f"luv: ready — {clone_dir.name}, branch {branch}")
    ensure_pr_rules()
    if nav_mode:
        navigate(clone_dir)
    elif resume_mode:
        resume(clone_dir)
    else:
        launch(clone_dir, prompt)


def main() -> None:
    args = sys.argv[1:]

    nav_mode = "-n" in args
    resume_mode = "-r" in args
    force = "-f" in args or "--force" in args
    args = [a for a in args if a not in ("-n", "-r", "-f", "--force")]

    if not args or args[0] in ("-h", "--help"):
        print("""\
Usage: luv [flags] <command>

Flags:
  -n            navigate: open a shell in the work folder instead of launching Claude
  -r            resume: resume the last Claude session in the work folder
  -f, --force   (with --clean) skip safety checks and delete all work folders

Commands:
  luv <repo> [prompt...]          create a new PR workspace for <repo>
  luv <repo> <number> [prompt]    reopen an existing work folder by number
  luv -l <PR URL> [prompt]        open any GitHub PR by URL (any org)
  luv <repo> -pr <number> [prompt] open a GitHub PR by repo + number
  luv --clean [-f]                delete fully-pushed work folders

Docker:
  If the repo contains .luv/settings.json with a "compose_file" key,
  luv starts a Docker Compose environment and runs Claude inside the
  "dev-environment" service. Torn down automatically on exit.""")
        sys.exit(0)

    if args[0] == "--clean":
        cmd_clean(force=force)
        return

    # luv -l <PR URL>
    if args[0] == "-l":
        if len(args) < 2:
            die("usage: luv -l <PR URL>")
        url = args[1]
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
        if not m:
            die(f"cannot parse PR URL: {url}")
        org, repo, number = m.group(1), m.group(2), int(m.group(3))
        prompt = " ".join(args[2:]) or None
        open_pr(org, repo, number, prompt, nav_mode, resume_mode)
        return

    repo = args[0].rstrip("/")
    if "/" in repo:
        die("pass only the repo name, not owner/repo")

    # luv <repo> -pr <number>
    if "-pr" in args:
        idx = args.index("-pr")
        if idx + 1 >= len(args):
            die("usage: luv <repo> -pr <number>")
        try:
            number = int(args[idx + 1])
        except ValueError:
            die(f"expected a PR number after -pr, got '{args[idx + 1]}'")
        prompt_parts = [a for i, a in enumerate(args) if i not in (0, idx, idx + 1)]
        prompt = " ".join(prompt_parts) or None
        open_pr(ORG, repo, number, prompt, nav_mode, resume_mode)
        return

    # Detect optional numeric second argument
    if len(args) > 1 and args[1].isdigit():
        number = int(args[1])
        prompt = " ".join(args[2:]) or None
        open_existing(repo, number, prompt, nav_mode, resume_mode)
        return

    prompt = " ".join(args[1:]) if len(args) > 1 else None

    # 1. Verify repo exists
    r = run(["gh", "api", f"repos/{ORG}/{repo}"])
    if r.returncode != 0:
        die(f"repo '{ORG}/{repo}' not found or gh auth failed.\n{r.stderr.strip()}")

    # 2. Get latest issue/PR number (shared counter on GitHub)
    r = run(["gh", "api",
             f"repos/{ORG}/{repo}/issues?state=all&per_page=1&sort=created&direction=desc"])
    if r.returncode != 0:
        die(f"failed to fetch issues.\n{r.stderr.strip()}")
    items = json.loads(r.stdout)
    latest = items[0]["number"] if items else 0
    candidate = latest + 1

    # 3. Find free local folder
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    while (PRS_DIR / f"{repo}-{candidate}").exists():
        candidate += 1
    clone_dir = PRS_DIR / f"{repo}-{candidate}"

    # 4. Clone
    clone_url = f"https://github.com/{ORG}/{repo}"
    print(f"luv: cloning {clone_url} -> {clone_dir}")
    r = subprocess.run(["git", "clone", clone_url, str(clone_dir)])
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")

    # 5. Create branch
    branch = f"luv-{candidate}"
    print(f"luv: creating branch {branch}")
    r = subprocess.run(["git", "checkout", "-b", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout -b failed (exit {r.returncode})")

    # 6. Ensure PR rules in ~/.claude/CLAUDE.md
    ensure_pr_rules()

    print(f"luv: ready — {clone_dir.name}, branch {branch}")

    # 7. Launch claude, resume session, or open shell (replace this process)
    if nav_mode:
        navigate(clone_dir)
    elif resume_mode:
        resume(clone_dir)
    else:
        launch(clone_dir, prompt)
