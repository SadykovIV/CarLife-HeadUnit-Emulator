"""HiCar JSON business stream with optional native HiChain authentication."""
import codecs
import json

MAX_MESSAGE = 65536


class AuthError(ValueError):
    pass


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise AuthError('duplicate JSON field')
        obj[key] = value
    return obj


def parse_json(text):
    try:
        return json.loads(text, object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(AuthError('invalid JSON constant')))
    except (ValueError, RecursionError) as error:
        raise AuthError('invalid JSON message') from error


class JsonStream:
    """Incremental UTF-8 object framing, including escaped braces and coalescing."""
    def __init__(self):
        self.utf8 = codecs.getincrementaldecoder('utf-8')()
        self.parts = []
        self.depth = 0
        self.quoted = self.escaped = False
        self.size = 0
        self.failed = False

    def feed(self, data):
        if self.failed:
            raise AuthError('JSON stream requires reset')
        result = []
        try:
            for char in self.utf8.decode(data):
                if not self.parts and char.isspace():
                    continue
                if not self.parts and char != '{':
                    raise AuthError('expected JSON object')
                self.parts.append(char)
                self.size += len(char.encode('utf-8'))
                if self.size > MAX_MESSAGE:
                    raise AuthError('JSON message exceeds size limit')
                if self.quoted:
                    if self.escaped:
                        self.escaped = False
                    elif char == '\\':
                        self.escaped = True
                    elif char == '"':
                        self.quoted = False
                elif char == '"':
                    self.quoted = True
                elif char in '{[':
                    self.depth += 1
                    if self.depth > 32:
                        raise AuthError('JSON nesting exceeds limit')
                elif char in '}]':
                    self.depth -= 1
                    if self.depth == 0:
                        result.append(parse_json(''.join(self.parts)))
                        self.parts.clear()
                        self.size = 0
        except (AuthError, UnicodeError) as error:
            self.failed = True
            self.parts.clear()
            raise AuthError('invalid JSON business stream') from error
        return result

    def finish(self):
        try:
            self.utf8.decode(b'', final=True)
        except UnicodeError as error:
            raise AuthError('incomplete UTF-8 business stream') from error
        if self.parts:
            raise AuthError('incomplete JSON business stream')
