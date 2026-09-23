"""Standard-library tool worker, executed inside Docker (never against the host)."""
from __future__ import annotations

import json
import ctypes
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

LIMIT = 2_000_000


def safe_path(root: Path, value: str) -> Path:
    requested = Path(value)
    if requested.is_absolute():
        requested = requested.relative_to(root)
    if '..' in requested.parts or '.git' in requested.parts:
        raise ValueError('Path escapes repository or accesses Git metadata')
    path = (root / requested).resolve()
    if not path.is_relative_to(root) or '.git' in path.relative_to(root).parts:
        raise ValueError('Path escapes repository or accesses Git metadata')
    return path


def run_command(command: str, timeout_seconds: int = 120) -> dict:
    if not isinstance(command, str) or not command.strip():
        raise ValueError('command must be a nonempty string')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ValueError('timeout_seconds must be between 1 and 300')
    start = time.monotonic()
    # Adopt orphaned descendants so even a child using setsid is cleaned up.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER on Linux
        raise OSError(ctypes.get_errno(), 'Could not supervise command descendants')
    process = subprocess.Popen(['sh', '-lc', command], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True)
    chunks = []
    size = 0
    timed_out = truncated = False
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = timeout_seconds - (time.monotonic() - start)
            if remaining <= 0:
                timed_out = True
                break
            ready = selector.select(min(remaining, 0.2))
            for key, _ in ready:
                data = os.read(key.fd, min(65536, LIMIT - size + 1))
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                chunks.append(data[:LIMIT - size])
                size += len(data)
                if size > LIMIT:
                    truncated = True
                    break
            if truncated:
                break
        if not timed_out and not truncated:
            try:
                process.wait(timeout=max(0.01, timeout_seconds - (time.monotonic() - start)))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        # Also clean up children which closed stdout and outlived the shell.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for _ in range(20):
            children = []
            for proc in Path('/proc').glob('[0-9]*/status'):
                try:
                    ppid = next(line for line in proc.read_text().splitlines() if line.startswith('PPid:'))
                    if int(ppid.split()[1]) == os.getpid():
                        children.append(int(proc.parent.name))
                except (OSError, StopIteration, ValueError):
                    continue
            if not children:
                break
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
        selector.close()
        process.stdout.close()
    output = b''.join(chunks).decode('utf-8', errors='replace')
    if truncated:
        output += '\n[output collection stopped at 2,000,000 bytes]'
    return {'exit_code': 124 if timed_out else process.returncode,
            'seconds': round(time.monotonic() - start, 3), 'timed_out': timed_out,
            'truncated': truncated, 'output': output}


def execute(root: Path, operation: str, arguments: dict) -> dict:
    root = root.resolve()
    if operation == 'Run':
        result = run_command(**arguments)
        status = subprocess.run(['git', '-c', 'core.fsmonitor=false', 'status', '--porcelain',
                                 '--untracked-files=normal'], capture_output=True, text=True, timeout=5)
        result['workspace_status'] = (status.stdout[:12000] if status.returncode == 0
                                      else 'Git status unavailable')
        if len(status.stdout) > 12000:
            result['workspace_status'] += '\n[status truncated; inspect repository for more changes]'
        return result
    path = safe_path(root, arguments.get('file_path', arguments.get('path', '.')))
    relative = str(path.relative_to(root))
    if operation == 'Resolve':
        return {'path': relative}
    if operation in {'Read', 'Write', 'Edit'}:
        if operation == 'Read':
            ranges = arguments.get('lines_range')
            if ranges is None and path.stat().st_size > LIMIT:
                raise ValueError('File exceeds 2 MB tool limit; request a line range')
            if ranges is not None:
                if not isinstance(ranges, list) or not ranges:
                    raise ValueError('lines_range must contain at least one range')
                for pair in ranges:
                    if (not isinstance(pair, list) or len(pair) != 2 or
                            any(type(n) is not int for n in pair) or pair[0] < 1 or pair[1] < pair[0]):
                        raise ValueError('Ranges must be 1-based inclusive [start, end] pairs')
            selected = [[] for _ in ranges] if ranges else [[]]
            count = size = 0
            with path.open(encoding='utf-8') as stream:
                # Bounded readline also handles a multi-megabyte single line safely.
                while True:
                    line = stream.readline(LIMIT + 1)
                    if not line:
                        break
                    count += 1
                    targets = [0] if ranges is None else [i for i, (start, end) in
                                enumerate(ranges) if start <= count <= end]
                    if len(line.encode()) > LIMIT or (len(line) > LIMIT and not line.endswith('\n')):
                        raise ValueError('A single line exceeds the 2 MB tool limit')
                    for index in targets:
                        text = f'{count}: {line}'
                        size += len(text.encode())
                        if size > LIMIT * 2:
                            raise ValueError('Selected content exceeds tool limit; use narrower ranges')
                        selected[index].append(text)
            return {'output': f'File: {relative} ({count} lines, {path.stat().st_size} bytes)\n' +
                    ''.join(line for block in selected for line in block), 'path': relative}
        content = arguments['content']
        if not isinstance(content, str) or len(content.encode()) > LIMIT:
            raise ValueError('Content must be text of at most 2 MB')
        ranges = arguments.get('line_range')
        if operation == 'Edit' and ranges is None:
            raise ValueError('Edit requires line_range')
        if ranges is not None:
            if len(ranges) != 1 or len(ranges[0]) != 2:
                raise ValueError('Specify one inclusive line range')
            lines = path.read_text().splitlines(keepends=True)
            start, end = ranges[0]
            if any(type(n) is not int for n in (start, end)) or not 1 <= start <= end <= len(lines):
                raise ValueError('Invalid edit range')
            content = ''.join(lines[:start - 1]) + content + ''.join(lines[end:])
        if len(content.encode()) > LIMIT:
            raise ValueError('Resulting file exceeds 2 MB')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return {'output': f'Wrote {relative}', 'path': relative}
    if operation not in {'List', 'Search'}:
        raise ValueError(f'Unknown tool: {operation}')
    if not path.is_dir():
        raise ValueError('path must be a directory')
    query = arguments.get('query')
    if operation == 'Search' and (not isinstance(query, str) or not query):
        raise ValueError('Search requires a nonempty literal query')
    output = []
    size = 0
    visited = 0
    truncated = False
    recursive = arguments.get('recursive', operation == 'Search')
    for folder, directories, files in os.walk(path, followlinks=False):
        directories[:] = sorted(d for d in directories if d != '.git' and
                                not (Path(folder) / d).is_symlink())
        names = sorted(directories + files) if operation == 'List' else sorted(files)
        for name in names:
            item = Path(folder) / name
            if item.is_symlink() or name == '.git':
                continue
            visited += 1
            label = str(item.relative_to(root))
            if operation == 'List':
                matches = [label + ('/' if item.is_dir() else '') + '\n']
            elif item.stat().st_size <= LIMIT:
                try:
                    matches = [f'{label}:{i}: {line}\n' for i, line in
                               enumerate(item.read_text(encoding='utf-8').splitlines(), 1)
                               if query in line]
                except (UnicodeError, OSError):
                    matches = []
            else:
                matches = [f'[Skipped file over 2 MB: {label}]\n']
            for match in matches:
                if size + len(match.encode()) > LIMIT:
                    truncated = True
                    break
                output.append(match)
                size += len(match.encode())
            if truncated or visited >= 10000:
                truncated = True
                break
        if truncated or not recursive:
            break
    if truncated:
        output.append('[collection limit reached; narrow the path or query]\n')
    return {'output': ''.join(output), 'truncated': truncated}


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        print(json.dumps(execute(Path('/repo'), request['operation'], request['arguments'])))
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(json.dumps({'error': f'{type(error).__name__}: {error}'}))
