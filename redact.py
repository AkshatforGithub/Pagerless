import re

RULES = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),
    (re.compile(r"\b(?:gsk|sk|ghp|xox[bap])[-_][A-Za-z0-9_-]{16,}"), "***"),
    (re.compile(r"(://[^:/\s]+:)[^@\s]+(@)"), r"\1***\2"),
    (re.compile(r"""(?i)(password|passwd|pwd|secret|token|api[_-]?key|authorization)(["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s,;]+)"""), r"\1\2***"),
]


def redact(text: str) -> str:
    for pat, rep in RULES:
        text = pat.sub(rep, text)
    return text
