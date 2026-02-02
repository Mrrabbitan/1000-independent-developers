#!/usr/bin/env python3
import datetime as dt
import json
import os
import re
import sys
import time
import subprocess
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


AUTO_START = "<!-- AUTO-GENERATED START -->"
AUTO_END = "<!-- AUTO-GENERATED END -->"
TOKEN_PLACEHOLDER = "__SET_GITHUB_TOKEN__"

TABLE_HEADER = "| 类别 | 开发者 | 项目名称 | 链接 | 简介 |"
TABLE_DIVIDER = "|---|---|---|---|---|"


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def load_config(path: str) -> dict:
    raw = read_text(path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore

        return yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "无法解析配置文件，请使用 JSON 语法或安装 PyYAML"
        ) from exc


def http_get_json(url: str, headers: dict) -> dict:
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        hint = ""
        if exc.code == 403:
            hint = "（可能触发限流，请设置 GITHUB_TOKEN 或先执行 gh auth login）"
        raise RuntimeError(f"GitHub API 错误 {exc.code}: {body}{hint}") from exc
    except URLError as exc:
        raise RuntimeError(f"网络请求失败: {exc}") from exc


def get_gh_cli_token() -> str:
    try:
        output = subprocess.check_output(
            ["gh", "auth", "token"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return output.strip()
    except Exception:
        return ""


def parse_markdown_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if line.startswith("|---"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 5:
            continue
        if cols[0] == "类别" and cols[1] == "开发者":
            continue
        rows.append(cols[:5])
    return rows


def extract_url(cell: str) -> str:
    match = re.search(r"\((https?://[^)]+)\)", cell)
    if match:
        return match.group(1)
    if cell.startswith("http://") or cell.startswith("https://"):
        return cell
    return ""


def sanitize_cell(text: str) -> str:
    return text.replace("|", "／").replace("\n", " ").strip()


def truncate_text(text: str, max_len: int) -> str:
    if max_len <= 0:
        return text
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def is_excluded_text(text: str, exclude_keywords: list[str]) -> bool:
    text_lower = text.lower()
    for kw in exclude_keywords:
        if kw.lower() in text_lower:
            return True
    return False


def classify_category(repo: dict, categories: list[dict], default: str) -> str:
    topics = repo.get("topics") or []
    haystack = " ".join(
        [
            repo.get("name", ""),
            repo.get("description", "") or "",
            " ".join(topics),
        ]
    ).lower()
    for category in categories:
        for kw in category.get("keywords", []):
            if kw.lower() in haystack:
                return category.get("name", default)
    return default


def is_excluded_repo(repo: dict, exclude_keywords: list[str], exclude_topics: list[str]) -> bool:
    name = repo.get("name", "") or ""
    description = repo.get("description", "") or ""
    text = f"{name} {description}".lower()
    for kw in exclude_keywords:
        if kw.lower() in text:
            return True
    topics = [t.lower() for t in (repo.get("topics") or [])]
    exclude_topics_set = {t.lower() for t in exclude_topics}
    for topic in topics:
        if topic in exclude_topics_set:
            return True
    return False


def parse_iso_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_user_location(login: str, headers: dict, api_base: str, cache: dict) -> str:
    if login in cache:
        return cache[login]
    url = f"{api_base}/users/{login}"
    data = http_get_json(url, headers=headers)
    location = (data.get("location") or "").strip()
    cache[login] = location
    return location


def search_repositories(config: dict, headers: dict) -> list[dict]:
    github_cfg = config["github"]
    api_base = github_cfg["api_base"].rstrip("/")
    per_page = int(github_cfg.get("per_page", 100))
    max_pages = int(github_cfg.get("max_pages", 1))
    sleep_seconds = float(github_cfg.get("sleep_seconds", 0))

    repos = []
    seen_ids = set()
    for query in config["queries"]:
        q = query["q"]
        sort = query.get("sort", "stars")
        order = query.get("order", "desc")
        for page in range(1, max_pages + 1):
            params = {
                "q": q,
                "sort": sort,
                "order": order,
                "per_page": per_page,
                "page": page,
            }
            url = f"{api_base}/search/repositories?{urlencode(params)}"
            data = http_get_json(url, headers=headers)
            items = data.get("items", [])
            if not items:
                break
            for repo in items:
                repo_id = repo.get("id")
                if repo_id in seen_ids:
                    continue
                seen_ids.add(repo_id)
                repos.append(repo)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return repos


def build_table_rows(repos: list[dict], config: dict, headers: dict) -> list[list[str]]:
    github_cfg = config["github"]
    filters = config.get("filters", {})
    min_stars = int(github_cfg.get("min_stars", 0))
    pushed_days = int(github_cfg.get("pushed_within_days", 365))
    include_location = bool(github_cfg.get("include_owner_location", False))
    max_desc_len = int(github_cfg.get("max_description_length", 0))
    max_new = int(github_cfg.get("max_new_per_run", 50))
    exclude_keywords = filters.get("exclude_keywords", [])
    exclude_topics = filters.get("exclude_topics", [])
    prefer_homepage = bool(filters.get("prefer_homepage", False))
    require_homepage = bool(filters.get("require_homepage", False))
    category_default = config.get("category_default", "其他工具")
    categories = config.get("categories", [])
    api_base = github_cfg["api_base"].rstrip("/")

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=pushed_days)
    candidates = []
    user_cache = {}

    for repo in repos:
        if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
            continue
        if is_excluded_repo(repo, exclude_keywords, exclude_topics):
            continue
        owner = repo.get("owner") or {}
        if owner.get("type") != "User":
            continue
        if int(repo.get("stargazers_count", 0)) < min_stars:
            continue
        pushed_at = repo.get("pushed_at")
        if not pushed_at:
            continue
        pushed_time = parse_iso_time(pushed_at)
        if pushed_time < cutoff:
            continue

        owner_login = owner.get("login", "").strip()
        owner_display = owner_login
        if include_location and owner_login:
            location = fetch_user_location(owner_login, headers, api_base, user_cache)
            if location:
                owner_display = f"{owner_login}({location})"

        project_name = repo.get("name") or owner_login
        homepage = (repo.get("homepage") or "").strip()
        if homepage.startswith("http://") or homepage.startswith("https://"):
            project_url = homepage
        else:
            project_url = repo.get("html_url", "")
        has_homepage = bool(homepage)
        if require_homepage and not has_homepage:
            continue

        description = repo.get("description") or "暂无简介"
        description = truncate_text(description, max_desc_len)
        category = classify_category(repo, categories, category_default)

        row = [
            sanitize_cell(category),
            sanitize_cell(owner_display),
            sanitize_cell(project_name),
            sanitize_cell(f"[{project_name}]({project_url})"),
            sanitize_cell(description),
        ]

        candidates.append(
            {
                "row": row,
                "has_homepage": has_homepage,
                "stars": int(repo.get("stargazers_count", 0)),
                "pushed_at": pushed_time,
            }
        )

    if prefer_homepage:
        candidates.sort(
            key=lambda item: (item["has_homepage"], item["stars"], item["pushed_at"]),
            reverse=True,
        )
    else:
        candidates.sort(
            key=lambda item: (item["stars"], item["pushed_at"]),
            reverse=True,
        )

    return [item["row"] for item in candidates[:max_new]]


def build_table_text(rows: list[list[str]]) -> str:
    lines = [TABLE_HEADER, TABLE_DIVIDER]
    for row in rows:
        padded = row + [""] * (5 - len(row))
        lines.append("| " + " | ".join(padded[:5]) + " |")
    return "\n".join(lines)


def update_readme(
    readme_path: str,
    new_rows: list[list[str]],
    max_total: int,
    max_desc_len: int,
    exclude_keywords: list[str],
    prune_existing: bool,
) -> int:
    content = read_text(readme_path)
    start = content.find(AUTO_START)
    end = content.find(AUTO_END)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("README 未找到自动更新标记区块")

    block = content[start + len(AUTO_START) : end]
    existing_rows = parse_markdown_rows(block)
    if prune_existing and exclude_keywords:
        filtered_existing = []
        for row in existing_rows:
            project = row[2] if len(row) > 2 else ""
            desc = row[4] if len(row) > 4 else ""
            if is_excluded_text(f"{project} {desc}", exclude_keywords):
                continue
            filtered_existing.append(row)
        existing_rows = filtered_existing
    existing_urls = {extract_url(r[3]) for r in existing_rows if extract_url(r[3])}

    filtered_new = []
    for row in new_rows:
        url = extract_url(row[3])
        if url and url in existing_urls:
            continue
        filtered_new.append(row)

    def normalize_row(row: list[str]) -> list[str]:
        category, developer, project, link, desc = (row + [""] * 5)[:5]
        desc = truncate_text(desc, max_desc_len)
        return [
            sanitize_cell(category),
            sanitize_cell(developer),
            sanitize_cell(project),
            sanitize_cell(link),
            sanitize_cell(desc),
        ]

    merged_rows = [normalize_row(r) for r in (filtered_new + existing_rows)]
    if max_total and len(merged_rows) > max_total:
        merged_rows = merged_rows[:max_total]

    table_text = build_table_text(merged_rows)
    new_block = f"{AUTO_START}\n{table_text}\n{AUTO_END}"
    new_content = content[:start] + new_block + content[end + len(AUTO_END) :]
    write_text(readme_path, new_content)
    return len(filtered_new)


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    readme_path = os.path.join(repo_root, "README.md")

    config = load_config(config_path)
    github_cfg = config["github"]
    filters = config.get("filters", {})

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or ""
    if token == TOKEN_PLACEHOLDER:
        token = ""
    if not token:
        token = get_gh_cli_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "indie-project-updater",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        repos = search_repositories(config, headers)
        new_rows = build_table_rows(repos, config, headers)
        added = update_readme(
            readme_path,
        new_rows,
        int(github_cfg.get("max_total", 0)),
        int(github_cfg.get("max_description_length", 0)),
        filters.get("exclude_keywords", []),
        bool(filters.get("prune_existing", False)),
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"新增条目: {added}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
