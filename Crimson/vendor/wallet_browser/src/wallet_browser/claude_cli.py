"""Structured answers from the local Claude Code CLI (`claude -p`).

Lets the crawler run on a Claude Code subscription login instead of an API
key. Every call hands the CLI a prompt (plus optional PNG screenshots, e.g. of
an image CAPTCHA) and a JSON schema, and returns the parsed object
(`claude -p --json-schema`).

Every CLI call is locked down (no tools, no MCP, no settings/hooks, no skills,
empty cwd) because the prompt carries untrusted page content -- the model must
only answer, never act on this machine.
"""
import asyncio
import json
import os
import re
import tempfile
from typing import Any


# ajv (the CLI's schema validator) runs in strict mode and rejects a few
# keywords schemas sometimes carry. Strip them recursively.
_STRIP_KEYS = {'min_items', 'max_items', '$schema', 'additionalItems'}


def _sanitize_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _sanitize_schema(v) for k, v in node.items() if k not in _STRIP_KEYS}
    if isinstance(node, list):
        return [_sanitize_schema(v) for v in node]
    return node


class ClaudeRefusal(RuntimeError):
    """Claude declined the step (a safety classifier or policy refusal). Callers stop
    the site and record it rather than retrying or rewording."""

    def __init__(self, detail: str, step: str = ''):
        super().__init__(detail)
        self.detail = detail
        self.step = step


# How a refusal reads when it comes back as an ordinary answer instead of a
# refusal stop_reason, e.g. an act() "reason" of "...halted by a safety classifier...".
_REFUSAL_RE = re.compile(r'safety classifier|halted by a? ?safety', re.I)


def _refusal(events: list[dict], result: dict | None) -> str | None:
    for ev in events:
        msg = ev.get('message') if isinstance(ev.get('message'), dict) else {}
        if 'refusal' in (ev.get('stop_reason'), msg.get('stop_reason')):
            return 'stop_reason=refusal'
    texts = [str((result or {}).get('result') or ''), *((result or {}).get('errors') or []),
             json.dumps((result or {}).get('structured_output') or '')]
    for t in texts:
        if m := _REFUSAL_RE.search(t):
            return t[max(0, m.start() - 80):m.end() + 80].strip()
    return None


def claude_bin() -> str:
    return os.environ.get('CLAUDE_BIN', 'claude')


async def ask_json(system_prompt: str, prompt: str, schema: dict, model: str,
                   images: list[str] | None = None) -> dict:
    """Run one locked-down `claude -p` turn and return its structured output.
    images: base64-encoded PNGs, attached after the prompt text in order."""
    # Read at call time so a .env loaded by the CLI entry point applies.
    step_timeout = float(os.environ.get('WALLET_BROWSER_STEP_TIMEOUT', '240'))
    args = [
        claude_bin(), '-p',
        '--input-format', 'stream-json',
        '--output-format', 'stream-json', '--verbose',
        '--model', model,
        '--tools', '',
        '--strict-mcp-config',
        '--setting-sources', '',
        '--disable-slash-commands',
        '--no-session-persistence',
        '--system-prompt', system_prompt,
        '--json-schema', json.dumps(_sanitize_schema(schema)),
    ]
    blocks = [{'type': 'text', 'text': prompt}]
    blocks += [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': b}}
               for b in images or []]
    stdin = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': blocks}}) + '\n'
    with tempfile.TemporaryDirectory(prefix='wallet-browser-claude-') as cwd:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout=step_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f'claude CLI timed out after {step_timeout}s')

    result = None
    events = []
    for line in out.decode(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
            if event.get('type') == 'result':
                result = event
    if why := _refusal(events, result):
        raise ClaudeRefusal(why)
    if not isinstance(result, dict) or result.get('is_error') or result.get('structured_output') is None:
        detail = (
            '; '.join((result or {}).get('errors') or [])
            or (result or {}).get('result')
            or err.decode(errors='replace').strip()
            or f'exit {proc.returncode}'
        )
        raise RuntimeError(f'claude CLI failed: {str(detail)[:500]}')
    return result['structured_output']
