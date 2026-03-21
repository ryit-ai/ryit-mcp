"""
Ryit MCP Server - Sync project documentation to ryit.ai

Tools:
  - scan_docs: Scan a directory for .md files
  - sync_docs: Create a space in ryit.ai from .md files (unpublished)
  - list_spaces: List all spaces for the current user
  - publish_space: Publish a space (makes it publicly visible)
  - unpublish_space: Unpublish a space
"""

import os
import uuid
import re
from pathlib import Path
from datetime import datetime, timezone

import frontmatter
import psycopg
from fastmcp import FastMCP

mcp = FastMCP(
    "ryit",
    instructions=(
        "Ryit MCP server for syncing project documentation to ryit.ai. "
        "Use scan_docs to find markdown files, sync_docs to create a space, "
        "and publish_space after user confirmation."
    ),
)

DATABASE_URL = os.environ.get(
    "RYIT_DATABASE_URL",
    "postgres://ryit:password@localhost:5432/ryit",
)

RYIT_USER_ID = os.environ.get("RYIT_USER_ID", "")


def get_conn():
    return psycopg.connect(DATABASE_URL)


def slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def ensure_user_id() -> str:
    if not RYIT_USER_ID:
        raise ValueError(
            "RYIT_USER_ID environment variable is required. "
            "Set it to your ryit.ai user UUID."
        )
    return RYIT_USER_ID


@mcp.tool()
def scan_docs(directory: str) -> str:
    """Scan a directory recursively for .md files and return a summary.

    Args:
        directory: Absolute path to the project directory to scan
    """
    path = Path(directory).expanduser().resolve()
    if not path.exists():
        return f"Error: Directory {path} does not exist"

    md_files = []
    for md_path in sorted(path.rglob("*.md")):
        # Skip common non-doc directories
        rel = md_path.relative_to(path)
        parts = rel.parts
        if any(
            p.startswith(".")
            or p in ("node_modules", "vendor", "venv", ".venv", "__pycache__", "dist", "build")
            for p in parts
        ):
            continue

        try:
            post = frontmatter.load(str(md_path))
            title = post.get("title", md_path.stem)
        except Exception:
            title = md_path.stem

        md_files.append(
            {
                "path": str(rel),
                "title": title,
                "size": md_path.stat().st_size,
            }
        )

    if not md_files:
        return f"No .md files found in {path}"

    lines = [f"Found {len(md_files)} markdown files in {path}:\n"]
    for f in md_files:
        lines.append(f"  - {f['path']} (title: {f['title']}, {f['size']} bytes)")

    lines.append(
        f"\nUse sync_docs to create a space from these files. "
        f"The space will NOT be published until you explicitly call publish_space."
    )
    return "\n".join(lines)


@mcp.tool()
def sync_docs(
    directory: str,
    space_title: str,
    space_slug: str = "",
    description: str = "",
) -> str:
    """Create a space in ryit.ai from markdown files in a directory.
    The space is created as UNPUBLISHED. Call publish_space to make it public.

    Args:
        directory: Absolute path to the project directory containing .md files
        space_title: Title for the new space
        space_slug: URL slug for the space (auto-generated from title if empty)
        description: Optional description for the space
    """
    user_id = ensure_user_id()
    path = Path(directory).expanduser().resolve()

    if not path.exists():
        return f"Error: Directory {path} does not exist"

    if not space_slug:
        space_slug = slugify(space_title)

    # Collect markdown files
    md_files = []
    for md_path in sorted(path.rglob("*.md")):
        rel = md_path.relative_to(path)
        parts = rel.parts
        if any(
            p.startswith(".")
            or p in ("node_modules", "vendor", "venv", ".venv", "__pycache__", "dist", "build")
            for p in parts
        ):
            continue
        md_files.append(md_path)

    if not md_files:
        return f"No .md files found in {path}"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Check if space slug already exists for this user
            cur.execute(
                "SELECT id FROM spaces WHERE user_id = %s AND slug = %s",
                (user_id, space_slug),
            )
            existing = cur.fetchone()
            if existing:
                space_id = existing[0]
                # Delete existing pages to re-sync
                cur.execute("DELETE FROM pages WHERE space_id = %s", (space_id,))
                cur.execute(
                    "UPDATE spaces SET title = %s, description = %s, updated_at = now() WHERE id = %s",
                    (space_title, description or None, space_id),
                )
            else:
                space_id = str(uuid.uuid4())
                cur.execute(
                    """INSERT INTO spaces (id, user_id, title, slug, description, is_published, "order")
                       VALUES (%s, %s, %s, %s, %s, false, 0)""",
                    (space_id, user_id, space_title, space_slug, description or None),
                )

            # Create pages from markdown files
            page_count = 0
            for order, md_path in enumerate(md_files):
                rel = md_path.relative_to(path)
                content = md_path.read_text(encoding="utf-8")

                try:
                    post = frontmatter.loads(content)
                    title = post.get("title", md_path.stem)
                    body = post.content
                except Exception:
                    title = md_path.stem
                    body = content

                page_slug = slugify(str(rel.with_suffix("")))
                if page_slug == "readme" or page_slug == "index":
                    page_slug = "index"
                    title = title if title != md_path.stem else space_title

                # Store as simple Tiptap JSON with a paragraph per line
                content_json = markdown_to_tiptap_json(body)

                page_id = str(uuid.uuid4())
                cur.execute(
                    """INSERT INTO pages
                       (id, space_id, title, slug, content_json, content_markdown, "order")
                       VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)""",
                    (
                        page_id,
                        space_id,
                        title,
                        page_slug,
                        content_json,
                        body,
                        order,
                    ),
                )
                page_count += 1

        conn.commit()
    finally:
        conn.close()

    return (
        f"Space '{space_title}' created with {page_count} pages (UNPUBLISHED).\n"
        f"Space ID: {space_id}\n"
        f"Slug: {space_slug}\n\n"
        f"To publish this space, ask the user for confirmation then call publish_space(space_id='{space_id}')."
    )


@mcp.tool()
def list_spaces() -> str:
    """List all documentation spaces for the current user."""
    user_id = ensure_user_id()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, slug, is_published, created_at
                   FROM spaces WHERE user_id = %s ORDER BY created_at DESC""",
                (user_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return "No spaces found."

    lines = [f"Found {len(rows)} spaces:\n"]
    for row in rows:
        status = "PUBLISHED" if row[3] else "DRAFT"
        lines.append(f"  [{status}] {row[1]} (slug: {row[2]}, id: {row[0]})")
    return "\n".join(lines)


@mcp.tool()
def publish_space(space_id: str) -> str:
    """Publish a space, making it publicly visible on ryit.ai.
    Only call this after getting explicit user confirmation.

    Args:
        space_id: The UUID of the space to publish
    """
    user_id = ensure_user_id()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, slug FROM spaces WHERE id = %s AND user_id = %s",
                (space_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return f"Error: Space {space_id} not found"

            title, slug = row

            cur.execute(
                "UPDATE spaces SET is_published = true, published_at = now(), updated_at = now() WHERE id = %s",
                (space_id,),
            )
        conn.commit()
    finally:
        conn.close()

    return (
        f"Space '{title}' is now PUBLISHED.\n"
        f"It will be available at https://<username>.ryit.ai/{slug} "
        f"after the content is built by the renderer."
    )


@mcp.tool()
def unpublish_space(space_id: str) -> str:
    """Unpublish a space, making it no longer publicly visible.

    Args:
        space_id: The UUID of the space to unpublish
    """
    user_id = ensure_user_id()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title FROM spaces WHERE id = %s AND user_id = %s",
                (space_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return f"Error: Space {space_id} not found"

            cur.execute(
                "UPDATE spaces SET is_published = false, updated_at = now() WHERE id = %s",
                (space_id,),
            )
        conn.commit()
    finally:
        conn.close()

    return f"Space '{row[0]}' is now UNPUBLISHED."


@mcp.tool()
def get_space_pages(space_id: str) -> str:
    """List all pages in a space.

    Args:
        space_id: The UUID of the space
    """
    user_id = ensure_user_id()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM spaces WHERE id = %s AND user_id = %s",
                (space_id, user_id),
            )
            if not cur.fetchone():
                return f"Error: Space {space_id} not found"

            cur.execute(
                """SELECT id, title, slug, "order"
                   FROM pages WHERE space_id = %s ORDER BY "order" ASC""",
                (space_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return "No pages in this space."

    lines = [f"Found {len(rows)} pages:\n"]
    for row in rows:
        lines.append(f"  {row[3]}. {row[1]} (slug: {row[2]}, id: {row[0]})")
    return "\n".join(lines)


@mcp.tool()
def delete_space(space_id: str) -> str:
    """Delete a space and all its pages.

    Args:
        space_id: The UUID of the space to delete
    """
    user_id = ensure_user_id()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title FROM spaces WHERE id = %s AND user_id = %s",
                (space_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return f"Error: Space {space_id} not found"

            cur.execute("DELETE FROM spaces WHERE id = %s", (space_id,))
        conn.commit()
    finally:
        conn.close()

    return f"Space '{row[0]}' and all its pages have been deleted."


def markdown_to_tiptap_json(markdown: str) -> str:
    """Convert markdown text to a simple Tiptap JSON document."""
    import json

    content = []
    lines = markdown.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2).strip()
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
            lang = line[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_text = "\n".join(code_lines)
            node = {
                "type": "codeBlock",
                "attrs": {"language": lang or None},
            }
            if code_text:
                node["content"] = [{"type": "text", "text": code_text}]
            content.append(node)
            continue

        # Empty lines
        if not line.strip():
            i += 1
            continue

        # Regular paragraphs
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            }
        )
        i += 1

    if not content:
        content = [{"type": "paragraph"}]

    doc = {"type": "doc", "content": content}
    return json.dumps(doc)


if __name__ == "__main__":
    mcp.run()
