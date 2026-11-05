"""Private, bounded deferred reply spool. All traversal uses no-follow directory FDs."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import secrets
import stat

ASK_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
LIMIT = 4000


def validate_id(ask):
    if not isinstance(ask, str) or not ASK_ID.fullmatch(ask):
        raise ValueError('Invalid reply identity')
    return ask + '.txt'


def check(info, directory=False):
    mode = 0o700 if directory else 0o600
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode
            or (not directory and (info.st_nlink != 1 or info.st_size > LIMIT * 4))):
        raise ValueError('Unsafe private reply path')


@contextmanager
def directory(create=False):
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
    if not runtime.is_absolute() or '..' in runtime.parts:
        raise ValueError('Invalid runtime path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in runtime.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        check(os.fstat(fd), True)
        for part in ('grokbot-desktop', 'replies'):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            check(os.fstat(fd), True)
        yield fd
    finally:
        os.close(fd)


def read_at(fd, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        check(os.fstat(stream.fileno()))
        data = stream.read(LIMIT * 4 + 1)
    answer = data.decode('utf-8')
    if len(data) > LIMIT * 4 or len(answer) > LIMIT:
        raise ValueError('Reply exceeds 4000 characters')
    return answer


def write(ask, answer):
    name = validate_id(ask)
    if len(answer) > LIMIT:
        raise ValueError('Reply exceeds 4000 characters')
    with directory(create=True) as fd:
        try:
            read_at(fd, name)
        except FileNotFoundError:
            pass
        temporary = '.' + secrets.token_hex(16)
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        try:
            with os.fdopen(handle, 'wb') as stream:
                check(os.fstat(stream.fileno()))
                stream.write(answer.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def consume(ask):
    name = validate_id(ask)
    try:
        with directory() as fd:
            answer = read_at(fd, name)
            os.unlink(name, dir_fd=fd)
            return answer
    except FileNotFoundError:
        return None
