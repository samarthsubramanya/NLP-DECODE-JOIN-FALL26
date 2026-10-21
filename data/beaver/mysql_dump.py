"""Strict lexer for mysqldump's literal-only INSERT grammar; never executes source SQL."""
import re
from decimal import Decimal

# Whole quoted tokens consume delimiters and MySQL escapes before punctuation is seen.
# Possessive runs (Python 3.11+) avoid per-character backtracking on long JSON text.
TOKEN = re.compile(
    r"\s+|--[^\n]*(?:\n|$)|/\*.*?\*/|'(?:[^'\\]++|\\.|'')*'|`(?:[^`]|``)*`|0x[0-9a-fA-F]+|[xX]'[0-9a-fA-F]*'|[bB]'[01]+'|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[A-Za-z_][A-Za-z_0-9]*|[(),;.=@+-]",
    re.S,
)
ESCAPES = {
    "0": "\0",
    "b": "\b",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "Z": "\x1a",
    "'": "'",
    '"': '"',
    "\\": "\\",
}


def tokens(sql):
    pos = 0
    while pos < len(sql):
        m = TOKEN.match(sql, pos)
        if not m:
            raise ValueError(f"Unsupported SQL token at {pos}: {sql[pos:pos+60]!r}")
        t = m.group()
        pos = m.end()
        if not t.isspace() and not t.startswith(("--", "/*")):
            yield t


def unquote(t):
    return t[1:-1].replace("``", "`")


def value(t):
    if len(t) == 4 and t.upper() == "NULL":
        return None
    if t.startswith("'"):
        s = t[1:-1]
        if "\\" not in s and "''" not in s:
            return s

        def decode_escape(match):
            text = match.group()
            if text == "''":
                return "'"
            escaped = text[1]
            return ESCAPES.get(escaped, text if escaped in "%_" else escaped)

        return re.sub(r"\\.|''", decode_escape, s, flags=re.S)
    if t.lower().startswith(("0x", "x'")):
        return bytes.fromhex(t[2:] if t[:2].lower() == "0x" else t[2:-1])
    if t.lower().startswith("b'"):
        return int(t[2:-1], 2)
    if re.fullmatch(r"[+-]?\d+", t):
        return int(t)
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", t):
        return Decimal(t)
    raise ValueError(f"Unsupported literal {t!r}")


def _insert_rows(sql):
    it = iter(tokens(sql))

    def expect(w):
        t = next(it, None)
        if t is None or t.upper() != w:
            raise ValueError(f"Expected {w}, got {t}")

    expect("INSERT")
    expect("INTO")
    name = next(it)
    if not name.startswith("`"):
        raise ValueError("Expected quoted table identifier")
    expect("VALUES")
    while True:
        expect("(")
        row = []
        while True:
            t = next(it, None)
            if t is None:
                raise ValueError("Truncated row")
            if t.startswith("_") and t.upper() == "_BINARY":
                t = next(it)
            row.append(value(t))
            sep = next(it, None)
            if sep == ")":
                break
            if sep != ",":
                raise ValueError(f"Expected column separator: {sep}")
        yield unquote(name), row
        sep = next(it, None)
        if sep == ";":
            if next(it, None) is not None:
                raise ValueError("Trailing SQL")
            return
        if sep != ",":
            raise ValueError(f"Expected row separator: {sep}")


def insert_rows(sql):
    match = re.match(r"INSERT INTO (`(?:[^`]|``)+`)", sql)
    context = unquote(match[1]) if match else "<unknown table>"
    try:
        yield from _insert_rows(sql)
    except (ValueError, RuntimeError) as error:
        raise ValueError(f"Table {context}: {error}") from error


def statements(stream, max_bytes=16 * 1024 * 1024):
    """Bound each source statement; quoted semicolons and multiline strings are safe.

    mysqldump emits INSERTs on physical lines. Accumulate lines until a terminating
    semicolon is outside strings/comments. Reject oversized statements explicitly.
    """
    pending = ""
    size = 0
    for raw in iter(lambda: stream.readline(max_bytes + 1), b""):
        if len(raw) > max_bytes:
            raise ValueError("SQL physical line exceeds memory limit")
        line = raw.decode("utf-8", errors="strict")
        if not pending and (not line.strip() or line.startswith("--")):
            continue
        if not pending and line.startswith("/*"):
            directive = line
            while "*/" not in directive:
                raw = stream.readline(max_bytes + 1)
                if not raw:
                    raise ValueError("Unterminated directive")
                directive += raw.decode("utf8")
                if len(directive.encode()) > max_bytes:
                    raise ValueError("Directive exceeds limit")
            if directive.split("*/", 1)[1].strip() not in ("", ";"):
                raise ValueError("Trailing SQL after directive")
            yield directive
            continue
        pending += line
        size += len(raw)
        if size > max_bytes:
            raise ValueError("SQL statement exceeds memory limit")
        # A complete lexical quoted token must cover every quote before terminator.
        if line.rstrip().endswith(";"):
            marks = list(
                re.finditer(
                    r"'(?:[^'\\]++|\\.|'')*'|`(?:[^`]|``)*`|/\*.*?\*/|--[^\n]*|;|'|`",
                    pending,
                    re.S,
                )
            )
            if any(m.group() in ("'", "`") for m in marks):
                continue
            semis = [m for m in marks if m.group() == ";"]
            if len(semis) != 1 or pending[semis[0].end() :].strip():
                raise ValueError("Unsupported or unterminated statement")
            yield pending
            pending = ""
            size = 0
    if pending.strip():
        raise ValueError("Unterminated SQL statement")


def parse_create(sql):
    m = re.match(r"CREATE TABLE `((?:[^`]|``)+)` \(\n(.*)\) (.*);\s*$", sql, re.S)
    if not m:
        raise ValueError("Unsupported CREATE TABLE grammar")
    cols = []
    constraints = []
    for line in m[2].splitlines():
        line = line.strip().rstrip(",")
        cm = re.match(
            r"`((?:[^`]|``)+)` ([a-z]+(?:\([^)]+\))?(?: unsigned)?)(.*)$", line
        )
        if cm:
            cols.append(
                dict(
                    name=cm[1].replace("``", "`"),
                    mysql_type=cm[2],
                    attributes=cm[3],
                    nullable="NOT NULL" not in cm[3],
                )
            )
        elif line.startswith(
            ("PRIMARY KEY", "UNIQUE KEY", "KEY", "CONSTRAINT", "FULLTEXT KEY", "CHECK")
        ):
            constraints.append(line)
        else:
            raise ValueError(f"Unsupported DDL: {line}")
    if len({col["name"].casefold() for col in cols}) != len(cols):
        raise ValueError("Case-colliding columns cannot be represented in DuckDB")
    return dict(
        name=m[1].replace("``", "`"),
        columns=cols,
        constraints=constraints,
        options=m[3],
        ddl=sql,
    )


def target_type(mysql):
    base = mysql.split("(")[0].split()[0]
    unsigned = mysql.endswith(" unsigned")
    if base in {
        "varchar",
        "char",
        "text",
        "mediumtext",
        "longtext",
        "tinytext",
        "enum",
    }:
        return "VARCHAR"
    if base in {"tinyint", "smallint", "mediumint", "int", "bigint"}:
        return ("U" if unsigned else "") + {
            "tinyint": "TINYINT",
            "smallint": "SMALLINT",
            "mediumint": "INTEGER",
            "int": "INTEGER",
            "bigint": "BIGINT",
        }[base]
    if base in {"datetime", "timestamp"}:
        return "TIMESTAMP"
    if base == "date":
        return "DATE"
    if base in {"blob", "longblob", "mediumblob", "tinyblob", "binary", "varbinary"}:
        return "BLOB"
    if base in {"decimal", "numeric"}:
        nums = [int(n) for n in re.findall(r"\d+", mysql)]
        if nums and nums[0] > 38:
            raise ValueError(f"Decimal precision exceeds 38: {mysql}")
        return mysql.upper().replace(" UNSIGNED", "")
    if base in {"float", "double"}:
        return "FLOAT" if base == "float" else "DOUBLE"
    raise ValueError(f"Unsupported MySQL type: {mysql}")
