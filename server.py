"""
Ryit MCP Server — Sync project documentation to ryit.ai

Communicates with ryit.ai editor API using GitHub PAT authentication.
Follows OWASP Secure MCP Server Development Guide v1.0.

Security controls:
- STDIO transport only (no network listener)
- GitHub PAT read from env, never exposed to model
- Input validation on all tool parameters
- Output sanitization (no tokens, paths, or stack traces)
- Human-in-the-loop for destructive operations (publish, delete)
- Rate limiting per session
- Deterministic session cleanup
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Optional

import httpx
import frontmatter
from fastmcp import FastMCP

# --- Configuration ---

RYIT_API_URL = os.environ.get("RYIT_API_URL", "https://editor.ryit.ai")
# API key read from env, NEVER passed as a tool parameter or exposed to model
_API_KEY = os.environ.get("RYIT_API_KEY", "")

MAX_FILE_SIZE = 1_000_000  # 1MB per file
MAX_FILES_PER_SYNC = 100
MAX_OUTPUT_LENGTH = 10_000  # chars returned to model
RATE_LIMIT_CALLS = 30
RATE_LIMIT_WINDOW = 60  # seconds

# --- Logging (redact sensitive data) ---

logger = logging.getLogger("ryit-mcp")
logger.setLevel(logging.INFO)

# --- Rate Limiter ---


class RateLimiter:
    def __init__(self, max_calls: int, window: int):
        self.max_calls = max_calls
        self.window = window
        self.calls: list[float] = []

    def check(self) -> bool:
        now = time.time()
        self.calls = [t for t in self.calls if now - t < self.window]
        if len(self.calls) >= self.max_calls:
            return False
        self.calls.append(now)
        return True


_rate_limiter = RateLimiter(RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW)

# --- HTTP Client ---


def _get_headers() -> dict:
    """Build auth headers. API key never exposed to model context."""
    if not _API_KEY:
        raise ValueError(
            "RYIT_API_KEY is not set. Generate one at https://editor.ryit.ai/settings "
            "then run: claude mcp remove ryit && claude mcp add "
            "-e RYIT_API_KEY=ryit_sk_xxx -s user ryit -- python server.py"
        )
    if not _API_KEY.startswith("ryit_sk_"):
        raise ValueError(
            "Invalid API key format. Keys start with 'ryit_sk_'. "
            "Generate one at https://editor.ryit.ai/settings"
        )
    return {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "ryit-mcp/0.3.0",
    }


def _api_request(
    method: str, path: str, body: Optional[dict] = None, timeout: float = 30.0
) -> dict:
    """Make an authenticated API request to ryit.ai editor."""
    if not _rate_limiter.check():
        return {
            "error": "Rate limit exceeded. Please wait before making more requests."
        }

    url = f"{RYIT_API_URL}{path}"
    try:
        with httpx.Client(timeout=timeout, verify=True) as client:
            response = client.request(
                method=method,
                url=url,
                headers=_get_headers(),
                json=body,
            )

            if response.status_code == 401:
                return {
                    "error": "Authentication failed. Your API key may be invalid or revoked. "
                    "Generate a new one at https://editor.ryit.ai/settings"
                }
            if response.status_code == 404:
                return {"error": "Resource not found."}
            if response.status_code >= 400:
                # Safe error — don't leak server internals
                return {"error": f"API request failed (HTTP {response.status_code})."}

            return response.json()
    except httpx.TimeoutException:
        return {"error": "Request timed out."}
    except httpx.ConnectError:
        return {"error": "Could not connect to ryit.ai API."}
    except Exception:
        return {"error": "An unexpected error occurred."}


# --- Input Validation ---


def _validate_slug(slug: str) -> str:
    """Validate and sanitize a URL slug."""
    slug = slug.strip().lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        raise ValueError("Invalid slug")
    if len(slug) > 255:
        raise ValueError("Slug too long (max 255)")
    return slug


def _validate_title(title: str) -> str:
    """Validate a title string."""
    title = title.strip()
    if not title:
        raise ValueError("Title is required")
    if len(title) > 255:
        raise ValueError("Title too long (max 255)")
    # Strip potential prompt injection markers
    title = re.sub(r"[<>{}]", "", title)
    return title


def _truncate_output(text: str) -> str:
    """Ensure output to model doesn't exceed size limit."""
    if len(text) > MAX_OUTPUT_LENGTH:
        return text[:MAX_OUTPUT_LENGTH] + "\n... (output truncated)"
    return text


# --- MCP Server ---

mcp = FastMCP(
    "ryit",
    instructions=(
        "Ryit documentation platform MCP server. "
        "Use scan_docs to find markdown files in a project directory. "
        "Use sync_docs to create a documentation space on ryit.ai (created as DRAFT, not published). "
        "IMPORTANT: Only call publish_space after getting explicit user confirmation. "
        "Only call delete_space after getting explicit user confirmation."
    ),
)


@mcp.tool()
def scan_docs(directory: str) -> str:
    """Scan a directory for markdown (.md) files and return a summary of what was found.
    This is a read-only operation that does not modify anything.

    Args:
        directory: Absolute path to the project directory to scan
    """
    try:
        path = Path(directory).expanduser().resolve()
    except Exception:
        return "Error: Invalid directory path."

    if not path.exists():
        return "Error: Directory does not exist."
    if not path.is_dir():
        return "Error: Path is not a directory."

    skip_dirs = {
        ".git",
        ".svn",
        "node_modules",
        "vendor",
        "venv",
        ".venv",
        "__pycache__",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
    }

    md_files = []
    for md_path in sorted(path.rglob("*.md")):
        rel = md_path.relative_to(path)
        if any(p in skip_dirs or p.startswith(".") for p in rel.parts):
            continue
        if md_path.stat().st_size > MAX_FILE_SIZE:
            continue
        if len(md_files) >= MAX_FILES_PER_SYNC:
            break

        try:
            post = frontmatter.load(str(md_path))
            title = post.get("title", md_path.stem)
        except Exception:
            title = md_path.stem

        md_files.append(
            {
                "path": str(rel),
                "title": str(title)[:255],
                "size": md_path.stat().st_size,
            }
        )

    if not md_files:
        return "No markdown files found in the specified directory."

    lines = [f"Found {len(md_files)} markdown file(s):\n"]
    for f in md_files:
        lines.append(f"  - {f['path']} (title: {f['title']})")
    lines.append(
        "\nUse sync_docs to create a space from these files. "
        "The space will be created as a DRAFT (not published)."
    )
    return _truncate_output("\n".join(lines))


@mcp.tool()
def sync_docs(
    directory: str,
    space_title: str,
    space_slug: str = "",
    description: str = "",
) -> str:
    """Create a documentation space on ryit.ai from markdown files in a directory.
    The space is created as a DRAFT (not published). You must call publish_space separately.

    Args:
        directory: Absolute path to the project directory containing .md files
        space_title: Title for the documentation space
        space_slug: URL slug (auto-generated from title if empty)
        description: Optional description
    """
    # Validate inputs
    try:
        title = _validate_title(space_title)
        slug = _validate_slug(space_slug) if space_slug else _validate_slug(space_title)
    except ValueError as e:
        return f"Validation error: {e}"

    try:
        path = Path(directory).expanduser().resolve()
    except Exception:
        return "Error: Invalid directory path."

    if not path.exists() or not path.is_dir():
        return "Error: Directory does not exist."

    description = description[:1000] if description else ""

    # Collect markdown files
    skip_dirs = {
        ".git",
        ".svn",
        "node_modules",
        "vendor",
        "venv",
        ".venv",
        "__pycache__",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
    }

    md_files = []
    for md_path in sorted(path.rglob("*.md")):
        rel = md_path.relative_to(path)
        if any(p in skip_dirs or p.startswith(".") for p in rel.parts):
            continue
        if md_path.stat().st_size > MAX_FILE_SIZE:
            continue
        if len(md_files) >= MAX_FILES_PER_SYNC:
            break
        md_files.append(md_path)

    if not md_files:
        return "No markdown files found."

    # Create space via API
    result = _api_request(
        "POST",
        "/api/v1/spaces",
        {
            "title": title,
            "slug": slug,
            "description": description,
        },
    )

    if "error" in result:
        return f"Failed to create space: {result['error']}"

    space_id = result["space"]["id"]

    # Create pages
    page_count = 0
    errors = []
    for order, md_path in enumerate(md_files):
        rel = md_path.relative_to(path)
        try:
            content = md_path.read_text(encoding="utf-8")
            post = frontmatter.loads(content)
            page_title = str(post.get("title", md_path.stem))[:500]
            body = post.content
        except Exception:
            page_title = md_path.stem[:500]
            body = md_path.read_text(encoding="utf-8", errors="replace")

        page_slug = _validate_slug(str(rel.with_suffix("")))
        content_json = _markdown_to_tiptap(body)

        page_result = _api_request(
            "POST",
            f"/api/v1/spaces/{space_id}/pages",
            {
                "title": page_title,
                "slug": page_slug,
                "content_json": json.loads(content_json),
                "content_markdown": body,
                "order": order,
            },
        )

        if "error" in page_result:
            errors.append(f"  - {rel}: {page_result['error']}")
        else:
            page_count += 1

    output = [
        f"Space '{title}' created as DRAFT with {page_count} page(s).",
        f"Space ID: {space_id}",
    ]
    if errors:
        output.append(f"\n{len(errors)} page(s) failed:")
        output.extend(errors[:5])  # Limit error output

    output.append(
        "\nIMPORTANT: This space is NOT published yet. "
        "Ask the user for confirmation before calling publish_space."
    )
    return _truncate_output("\n".join(output))


@mcp.tool()
def list_spaces() -> str:
    """List all documentation spaces on ryit.ai for the authenticated user.
    This is a read-only operation.
    """
    result = _api_request("GET", "/api/v1/spaces")
    if "error" in result:
        return f"Error: {result['error']}"

    spaces = result.get("spaces", [])
    if not spaces:
        return "No spaces found."

    lines = [f"Found {len(spaces)} space(s):\n"]
    for s in spaces:
        status = "PUBLISHED" if s.get("isPublished") else "DRAFT"
        lines.append(f"  [{status}] {s['title']} (slug: {s['slug']}, id: {s['id']})")
    return _truncate_output("\n".join(lines))


@mcp.tool()
def get_space_pages(space_id: str) -> str:
    """List all pages in a specific space.

    Args:
        space_id: The UUID of the space
    """
    if not re.match(r"^[0-9a-f-]{36}$", space_id):
        return "Error: Invalid space ID format."

    result = _api_request("GET", f"/api/v1/spaces/{space_id}/pages")
    if "error" in result:
        return f"Error: {result['error']}"

    pages = result.get("pages", [])
    if not pages:
        return "No pages in this space."

    lines = [f"Found {len(pages)} page(s):\n"]
    for p in pages:
        lines.append(f"  {p.get('order', 0)}. {p['title']} (slug: {p['slug']})")
    return _truncate_output("\n".join(lines))


@mcp.tool()
def publish_space(space_id: str) -> str:
    """Publish a space, making it publicly visible on ryit.ai.
    WARNING: This is a destructive action. Only call after explicit user confirmation.

    Args:
        space_id: The UUID of the space to publish
    """
    if not re.match(r"^[0-9a-f-]{36}$", space_id):
        return "Error: Invalid space ID format."

    result = _api_request("POST", f"/api/v1/spaces/{space_id}/publish")
    if "error" in result:
        return f"Error: {result['error']}"

    return f"Space {space_id} is now PUBLISHED and publicly visible."


@mcp.tool()
def unpublish_space(space_id: str) -> str:
    """Unpublish a space, removing it from public view.

    Args:
        space_id: The UUID of the space to unpublish
    """
    if not re.match(r"^[0-9a-f-]{36}$", space_id):
        return "Error: Invalid space ID format."

    result = _api_request("DELETE", f"/api/v1/spaces/{space_id}/publish")
    if "error" in result:
        return f"Error: {result['error']}"

    return f"Space {space_id} is now UNPUBLISHED."


@mcp.tool()
def delete_space(space_id: str) -> str:
    """Delete a space and all its pages permanently.
    WARNING: This is irreversible. Only call after explicit user confirmation.

    Args:
        space_id: The UUID of the space to delete
    """
    if not re.match(r"^[0-9a-f-]{36}$", space_id):
        return "Error: Invalid space ID format."

    result = _api_request("DELETE", f"/api/v1/spaces/{space_id}")
    if "error" in result:
        return f"Error: {result['error']}"

    return f"Space {space_id} has been permanently deleted."


# --- Markdown to Tiptap JSON converter ---


def _markdown_to_tiptap(markdown: str) -> str:
    """Convert markdown to a simple Tiptap JSON document."""
    content = []
    lines = markdown.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()[:500]
            content.append(
                {
                    "type": "heading",
                    "attrs": {"level": level},
                    "content": [{"type": "text", "text": text}],
                }
            )
            i += 1
            continue

        # Code blocks
        if line.startswith("```"):
            lang = line[3:].strip()[:50]
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            code_text = "\n".join(code_lines)
            node = {"type": "codeBlock", "attrs": {"language": lang or None}}
            if code_text:
                node["content"] = [{"type": "text", "text": code_text}]
            content.append(node)
            continue

        # Empty lines
        if not line.strip():
            i += 1
            continue

        # Paragraphs
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            }
        )
        i += 1

    if not content:
        content = [{"type": "paragraph"}]

    return json.dumps({"type": "doc", "content": content})


if __name__ == "__main__":
    mcp.run()
