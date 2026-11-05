from __future__ import annotations

import re

from . import config as C

_SECRET_VALUE = r"(?=[A-Za-z0-9_\-./+]*\d)(?=[A-Za-z0-9_\-./+]*[A-Za-z])[A-Za-z0-9_\-./+]{12,}"
_PROSE_PREFIXES = (
    "zero",
    "setup",
    "repeat",
    "unique",
    "special",
    "weighted",
    "date",
    "per",
    "multi",
    "single",
    "double",
    "one",
    "two",
    "three",
    "four",
    "first",
    "second",
    "pre",
    "post",
)
# a prose compound starts its word and is no assignment value: `PASSWORD="one-pass horse-staple"` still redacts
_NOT_VALUE = r"(?<![-\w.=:'\"])(?<![=:]\s)(?<![=:]\s['\"])"
_ASSIGN = r"""(?:[A-Za-z_]\w*=(?:"[^"]*"|'[^']*'|\S*)\s+)*"""  # VAR=value words before a command
_EXTRA_TOKENS = (
    r"\b(?:gh[os]_[A-Za-z0-9_]+|npm_[A-Za-z0-9_]+|rk_live_[A-Za-z0-9_]+|hf_[A-Za-z0-9_]+"
    r"|SG\.[A-Za-z0-9_.-]+|ya29\.[A-Za-z0-9_.-]+)"
    r"|https://hooks\.slack\.com/services/[^\s'\"<>]+"
)
_RULES = [
    (
        "prompt injection",
        r"\b(?:ignore|disregard|forget|override)\s+(?:(?:all|any|the|your|of|these|my)\s+)*"
        r"(?:previous|prior|above|earlier|system)\s+(?:instructions|prompts?|rules)\b",
    ),
    # Qualifiers can bypass this rule or block plain prose. Only text touching an added line counts,
    # so a false positive asks for a rephrase of new text and never locks an existing file.
    ("prompt injection", r"\byou\s+are\s+now\b"),
    ("prompt injection", r"\bdisregard\s+(?:your|all|any)\s+(?:guidelines|rules|instructions|safety)\b"),
    # one `|` only: `a || b` runs b when a fails, it pipes nothing; `|&` pipes stderr too
    (
        "a download piped into a shell; write download and run as separate steps",
        r"\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|downloadstring|new-object\s+(system\.)?net\.webclient)"
        r"[^\n]*(?<!\|)\|(?!\|)&?\s*"
        + _ASSIGN
        + r"(?:sudo(?:\s+-[-\w=]+(?:\s+(?!-)\S+)?)*\s+)?"
        + _ASSIGN
        + r"(?:(?:/[\w.-]+)*/)?(?:env\s+"
        + _ASSIGN
        + r")?(?:(?:/[\w.-]+)*/)?"
        r"(?:(?:(?:ba|z|da|k)?sh|fish|source|iex|invoke-expression)\b|\.(?=\s)"
        r"|(?:python\d*(?:\.\d+)?|perl|ruby|node)(?=\s+-(?![\w-])|\s*$))",
    ),
    (  # PowerShell runs a download as code without a pipe: iex (irm URL), iex ((New-Object Net.WebClient)...)
        "a download run as code; write download and run as separate steps",
        r"(\b(iex|invoke-expression)(\s+-c\w*)?|\[scriptblock\]::create)\s*[(\"'$][\s(\"'&{$]*"
        r"(iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget|new-object\s+(system\.)?net\.webclient)\b",
    ),
    (  # a shell or interpreter given a download as its script: bash -lc "$(curl URL)", bash <(curl URL),
        # bash <<< "$(curl URL)", eval, source or . of one, python3 -c "$(curl URL)"
        "a download run as code; write download and run as separate steps",
        r"\b(?:(?:ba|z|da|k)?sh|fish)(?:\.exe)?(?:\s+-[-\w=]+(?:\s+(?![-<])\S+)?)*(?:\s+/dev/stdin)?\s+"
        r"(?:-\w*c\w*\s*[\"']?(?:\$\(|`)|<?\s*<\(|<<<\s*[\"']?(?:\$\(|`))\s*(?:curl|wget)\b"
        r"|(?:\beval\b|\bsource\b|(?:^|(?<=[\s;&|(`]))\.)(?:\s+/dev/stdin)?\s*(?:<<<\s*)?[\"']?(?:\$\(|<\(|`)\s*"
        r"(?:curl|wget)\b"
        r"|\b(?:python\d*(?:\.\d+)?|perl|ruby|node)\s+-[ec]\s*[\"']?\$\(\s*(?:curl|wget)\b",
    ),
    (
        "a secret-shaped value",
        rf"(?<![a-z0-9])(api[_-]?key|secret|password|passwd|token)(?:_[a-z0-9]+)*['\"]?\s*[:=]\s*['\"]?{_SECRET_VALUE}",
    ),
    (
        "a secret-shaped value",
        r"\b(sk-[A-Za-z0-9_\-]{20,}|sk_(live|test)_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|glpat-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|xox[baprs]-[A-Za-z0-9-]{10,})",
    ),
    ("a private key", r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"),
    (
        "hidden or direction-changing characters",
        "[\\u200b-\\u200f\\u2028-\\u202e\\u2060-\\u2064\\u2066-\\u206f\\u061c\\ufeff\\x85\\xad\\u034f\\u180e\\ufe00-\\ufe0d"
        "\\U000e0000-\\U000e007f\\U000e0100-\\U000e01ef]",
    ),
]
_PATTERNS = [(label, re.compile(rx, re.I | re.M)) for label, rx in _RULES]
_PATTERNS.append(("a secret-shaped value", re.compile(_EXTRA_TOKENS)))
_IMPERATIVE = re.compile(r"^\s*(always|never|do not|don't|make sure|remember to|you must)\b", re.I)


# Words that also mean something else (`pass 17 tests`, `token count`) need a value no word, name, version or date
# looks like: two digits, and three when it has a run of six lowercase letters (`including3regressions` is a word);
# `_` only in a mixed-case value (`Xk9_mP2q`, not `test_hook_224`); `pass` and `auth` not a hex hash. Strong words
# take 8 characters with a digit, or a whole quoted value, and may have `/` inside. None take code or path syntax.
# Every scan stays inside the value's own run, and no pattern uses a possessive or atomic group (Python 3.10).
_WEAK_KEYS = r"token|credentials?"
_WEAKER_KEYS = r"pass|auth"
_STRONG_KEYS = (
    r"password|passwd|passphrase|secret|api[-_]?key|apikey|access[-_]?key|client[-_]secret|auth[-_]token|bearer"
    r"|private[-_]?key"
)
_C = r"""[^\s'"/\\()\[\]{}<>=,:;$`*|_%]"""
_CU = _C.replace("|_%", "|%")  # the same with `_`
_END = r"""(?![^\s.,;!?)'"])"""
_RUN_END = r"""[.!?]*(?![^\s'"\\()\[\]{}<>=,:;$`*|%])"""  # the value's run ends here, but for a closing `.`
_SP = r"[^\S\r\n]+"  # a line break ends the pair: a `pass` closing a block does not label the next line
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_VERSION = rf"[\^~]?v?\d+(?:[.-]\d+)+(?:[-+][0-9a-z]+(?:\.[0-9a-z]+)*)?{_RUN_END}"  # `2026-10-27`, `^5.0.0-beta.25`
_ONE_CASE = r"(?-i:[a-z0-9.@!#+-]*_[a-z0-9_.@!#+-]*|[A-Z0-9.@!#+-]*_[A-Z0-9_.@!#+-]*)"
_ISSUE = r"(?-i:[A-Z][A-Z\d]+-\d+)" + _RUN_END  # `ENG-4021`; two words and digits (`ZEPHYR-QUOKKA-7731`) still count
# A value built from words names something (`Float32Array`, `np.float32`, `gpt-4.1-mini`, `db-credentials-v2`): words
# of two or more lowercase letters (capitalized, or after an acronym), acronyms, and digit groups no lowercase letter
# follows, joined by `-`, `_`, `.` or `/`; a part may start with a short head on digits (`Ed25519`, `v2`) or be a
# numeronym (`k8s`). No name has uppercase words with digits (`ZEPHYR-QUOKKA-7731`) or only hex letters with digits.
_DIGITS = r"\d+(?:st|nd|rd|th)?(?![\da-z])"  # `3rd-party`
_PART = rf"(?:[A-Z]*[a-z]{{2,}}(?![a-z])|[A-Z]{{2,}}(?![A-Za-z])|{_DIGITS})"
_SEG = rf"(?:[a-z]\d+[a-z](?![A-Za-z\d])|(?:[A-Z][a-z]?|[a-z]){_DIGITS}|{_PART}){_PART}*"
_NAME_CHECKS = r"(?![A-Z_./-]*\d[A-Z\d_./-]*(?![A-Za-z\d_./-]))(?![A-Fa-f_./-]*\d[A-Fa-f\d_./-]*(?![A-Za-z\d_./-]))"
_WORDY = _NAME_CHECKS + _SEG


def _name(parts: str, end: str) -> str:
    """A name of words ending where the value does: any number of parts, `+` two or more, `strong` two or a word."""
    tail = {"": r"(?:[-_./]" + _SEG + ")*", "+": r"(?:[-_./]" + _SEG + ")+"}.get(parts)
    if tail is None:
        tail = r"(?:(?:[-_./]" + _SEG + r")+|(?<=[a-z]{2}))"  # `Ed25519PrivateKey`, not `maple7Quartz9`
    return rf"(?-i:{_WORDY}{tail}){end}"


_FLAG_END = r"""[.,;!?)]*(?![^\s'"`])"""
# after `token`, `credentials`, `pass` and `auth` code is no value either: a dotted callable (`base64.b64encode`) or a
# Go or pytest test name (`TestFNV32a`, `test_FNV32a`)
_CALLABLE = rf"(?-i:{_NAME_CHECKS}[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+){_RUN_END}"
_TEST_NAME = rf"(?-i:(?:Test|Example|Fuzz)[A-Z0-9_][A-Za-z0-9_]*|test_[A-Za-z0-9_]+){_RUN_END}"
_WEAK_VALUE = (
    rf"(?=(?:{_CU}*?\d){{2}})(?:(?=(?:{_CU}*?\d){{3}})|(?!{_C}*?(?-i:[a-z]{{6}})))"
    rf"(?!{_VERSION}|(?=\d*[a-f])[0-9a-f]{{7,12}}{_RUN_END}|(?:sha\d*|md5)-|(?:pr)?#\d|{_CU}*?\d{{4}}-\d\d-\d\d|{_CU}*?{_UUID})"
    rf"(?!{_ONE_CASE}{_RUN_END}|{_ISSUE}|{_name('', _RUN_END)}|{_CALLABLE}|{_TEST_NAME})(?:{_CU}){{8,}}(?!{_CU}){_END}"
)
_WEAKER_VALUE = rf"(?!(?=\d*[a-f])[0-9a-f]{{7,64}}{_RUN_END})(?:{_WEAK_VALUE})"  # git hashes and sha256 digests
_STRONG_RUN = "(?:" + _CU + r"""|(?<=[^\s'"])/(?=[^\s'"/]))"""  # `/` only inside
_NO_PATH = rf"""(?!(?:{_CU}|/)*/(?:{_CU}|/)*(?:/|\.[A-Za-z]{{1,4}}{_END}))"""  # `docs/v2/x`, `a/b.txt`


# after a strong word only code is no value: a qualified name (`ed25519.Ed25519PrivateKey`) or a type with a code
# suffix (`PKCS7PrivateKeyTypes`), never a word with digits (`Winter2024Spring`) or a phrase (`correct-horse-battery`)
_SNAKE = rf"{_SEG}(?:_{_SEG})*"
_SSH_KEY_FILE = r"id_(?:rsa|dsa|ecdsa|ed25519)\b"  # a key's file, never the key: `private_key: id_ed25519`
_CODE_SUFFIX = r"(?:Array|Types?|Error|Exception|Config|Class|Field|Keys?)"
_CODE = (
    rf"(?-i:{_NAME_CHECKS}(?:{_SNAKE}(?:\.{_SNAKE})+|(?=[A-Z][A-Za-z\d]*{_CODE_SUFFIX}(?![A-Za-z\d])){_SEG})){_RUN_END}"
)


def _strong(quoted: bool, names: str, commas: bool = False) -> str:
    """Strong words take 8 characters with a digit that `names` does not match, or a whole quoted value."""
    chars = _CU.replace(",:", ":") if commas else _CU
    run = _STRONG_RUN.replace(_CU, chars)
    bare = rf"(?=(?:{chars}|/)*?\d)(?!{names})"
    placeholder = r"""(?:password|changeme|placeholder|redacted)['"]"""  # `password "password"`
    whole = rf"""(?<=['"])(?=(?:{chars}|/)*['"])(?!{placeholder})"""
    value = f"(?:{bare}|{whole})" if quoted else bare
    head = rf"(?!{_VERSION}|(?:sha\d*|md5)-|pr#|{_UUID}|{_SSH_KEY_FILE}{_RUN_END})" + _NO_PATH.replace(_CU, chars)
    return rf"(?i:{head}{value}{run}{{8,}}(?!{run}){_END})"


_STRONG_VALUE = _strong(quoted=True, names=_CODE)
# a hyphenated word ending in a credential keyword redacts like a flag (`deploy-password 5729`), except a prose
# compound starting its word (`zero-token check`, `one-pass SHA256`, `300,000-token`); `--one-pass x` is a flag
_CREDENTIAL_FLAG = (
    r"(--?(?:[A-Za-z0-9]+[-_.]){0,3}(?:password|passwd|secret|api[-_]?key|access[-_]?key|"
    + "".join(rf"(?<!{_NOT_VALUE}{w}-)" for w in _PROSE_PREFIXES)
    + "".join(rf"(?<!{_NOT_VALUE}\d{{{n}}}-)" for n in range(1, 7))
    + r"(?:pass|token))(?:=|\s+)['\"]?)[^\s'\"]+"
)
# `kubectl get secret db-creds-v2` names a resource, it does not hold a value
_NOT_RESOURCE = "".join(
    rf"(?<!\b{w}\s)" for w in ("get", "describe", "delete", "edit", "patch", "create", "apply", "label", "annotate")
)
# placeholders and test literals stand for a value in examples: `Bearer {token}`, `<token>`, `%s`, `${TOKEN}`,
# `$TOKEN`, `[REDACTED]`, `sk-proj-...`, `hf_******`, a value under 8 characters, or words without a digit
# (`test-token`, `YOUR_API_KEY`, `YourTokenHere`); a real token has digits or no word shape (`dXNlcjpwYXNz`)
_PLACEHOLDER = (
    r"(?![{<%$\[]|\.\.\.)(?![^\s'\"]*(?:\.\.\.|…|\*\*\*))(?=[^\s'\"]{8})"
    r"(?!(?![^\s'\"\d]*\d)" + _name("", r"(?![^\s'\"])") + ")"
)


def _user_pass(end: str) -> str:
    """The placeholder pair in usage text (`--auth user:pass`), or a user before a reference (`admin:$ADMIN_PASS`)."""
    return r"(?:user(?:name)?:pass(?:word)?" + end + r"|[^\s'\":=$<({]+:[$%<{])"


# A comma pairs name and value only inside a call, and a second header name is no credential value.
_CODE_HEADER_PAIR = (
    r"['\"](?:\s*\]\s*=\s*['\"]|\s*,\s*['\"](?!(?:"
    + "|".join(map(re.escape, (*C.REDACT_CODE_AUTH_HEADERS, *C.REDACT_CODE_VALUE_HEADERS, "cookie")))
    + r")['\"]))"
)
_CODE_HEADER = (
    r"((?:\b(?!(?:return|yield|await|in|not|and|or|if|elif|while|for|assert|lambda|else)\b)[A-Za-z_]\w*\s*\(\s*['\"]|(?<![\w-])(?=[\w-]+['\"]\s*\]))(?:"
    + "(?:"
    + "|".join(map(re.escape, C.REDACT_CODE_AUTH_HEADERS))
    + ")"
    + _CODE_HEADER_PAIR
    + r"(?:bearer|basic|token|digest)\s+|"
    + "(?:"
    + "|".join(map(re.escape, C.REDACT_CODE_VALUE_HEADERS))
    + ")"
    + _CODE_HEADER_PAIR
    + "))"
    + _PLACEHOLDER
    # a header-shaped value (`X-Request-ID`) is another name, not a credential
    + r"(?![A-Z][A-Za-z0-9]*(?:-[A-Z][A-Za-z0-9]*)+['\"])"
    + r"[^\s'\"]+(?=['\"])"
)
_USER_CREDENTIALS = rf"((?<!\S)--user(?:=|{_SP})['\"]?)[^\s:'\"]*:" + _strong(quoted=True, names=_CODE, commas=True)
_QUOTED_STRONG = (
    r"((?<![A-Za-z0-9.])"
    + _NOT_RESOURCE
    + rf"(?:{_STRONG_KEYS}){_SP}(?:(?:is|was|to){_SP})?(['\"]))"
    + _strong(quoted=True, names=_CODE, commas=True)
    + r"(?=\2)"
)
# Checking for a keyword separately avoids retrying every suffix for every keyword in one long word.
_KEY_ASSIGNMENT = (
    r"(\b(?:(?=\w*(?:token|secret|password|passwd|api_?key|access_?key|credential))\w*"
    r"|\w*(?:api|access)-key\w*)['\"]?\s*[:=]\s*['\"]?)[^\s'\"&,;]+"
)
# Consume failed URI runs too, so finditer cannot retry each overlapping scheme in the same run.
_URI_CREDENTIALS = (
    r"((?<![a-z0-9+.-])[0-9+.-]*[a-z][a-z0-9+.-]*://)"
    r"(?:[^/\s:@'\"]*:[^@\s'\"]+(?=@)|(?P<plain_uri>[^/\s:@'\"]*(?::[^@\s'\"]*)?))"
)


_REDACT = [
    re.compile(rx, re.I)
    for rx in (
        r"((?:proxy-)?authorization\s*[:=]\s*(?:(?:bearer|basic|token|digest)\s+)?)[^\s'\"]+",
        # a quoted header name (JSON, a dict) counts with its scheme only: `"authorization": "operator ok"` is prose
        r"((?:proxy-)?authorization['\"]\s*[:=]\s*['\"](?:bearer|basic|token|digest)\s+)" + _PLACEHOLDER + r"[^\s'\"]+",
        r"((?:x-api-key|api-key|cookie|x-auth-token)\s*[:=]\s*)[^\s'\"]+",
        _CODE_HEADER,
        r"(\s-u\s*['\"]?)[^\s:'\"]+:[^\s'\"]+",
        _USER_CREDENTIALS,
        _CREDENTIAL_FLAG,
        # a quoted passphrase of plain words (`password "correct horse battery staple"`), before the assignment rule
        # below, which would take its first word only
        r"((?<![A-Za-z0-9.])(?:[A-Za-z0-9]+[-_]){0,3}(?:password|passwd|passphrase|secret)['\"]?"
        rf"(?:{_SP}(?:(?:is|was|to){_SP})?|[^\S\r\n]*[:=][^\S\r\n]*)(['\"]))"
        r"(?-i:[a-z][a-z0-9]*(?: [a-z][a-z0-9]*){2,7})(?=\2(?![^\S\r\n]*[:=]))",  # a quoted key is no value
        _QUOTED_STRONG,
        _KEY_ASSIGNMENT,
        r"(https?://[^\s?#'\"]+)(?:\?[^\s'\"]*|(?P<plain_uri>))",
        _URI_CREDENTIALS,
        r"(\s-p)[^\s'\"]{3,}",  # mysql -p<password>
        r"()\b(?:sk-[A-Za-z0-9_\-]{20,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|glpat-[A-Za-z0-9_\-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_\-]{20,}"
        r"(?:\.[A-Za-z0-9_\-]+){0,2})",
        r"()-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|$)",
        # flags take any 8-character value, but `--auth X` and `--auth chatgpt` in backticked prose are names, and
        # `--no-auth` takes none; no value is a reference, usage text, another flag, a path, a mode, a metavar, a
        # name of two or more parts or `user:pass`
        r"(--(?!(?:no|skip|disable|without)[-_])(?:[A-Za-z0-9]+[-_.]){0,3}(?:auth|bearer|credentials?|private[-_]?key|passphrase)"
        rf"(?:=|{_SP})['\"]?)"
        r"(?![-<{$%\[]|\.{1,2}/|/|~/|(?:optional|required|none|basic|oauth2?|token|bearer|password|anonymous|disabled"
        r"|enabled)" + _FLAG_END + "|" + _user_pass(_FLAG_END) + r")(?![^\s'\"`=]*\.[A-Za-z]{1,5}(?![^\s'\"`]))"
        r"(?!(?-i:[A-Z][A-Z_-]*)[\])},.|>]*(?![^\s'\"`])|" + _name("+", _FLAG_END) + ")"
        r"[^\s'\"`]{8,}(?![^\s'\"`])(?!`)",
        rf"(\b(?:{_WEAK_KEYS}){_SP}['\"]?)(?:{_WEAK_VALUE})",
        rf"(\b(?:{_WEAKER_KEYS}){_SP}['\"]?)(?:{_WEAKER_VALUE})",
        r"((?<![A-Za-z0-9.])"
        + _NOT_RESOURCE
        + rf"(?:{_STRONG_KEYS}){_SP}(?:(?:is|was|to){_SP})?['\"]?)(?:{_STRONG_VALUE})",
        # a label alone on its line still pairs with the value on the next: `Password` then `Xk29fLq83mZp`
        rf"((?:^|(?<=\n))[^\S\r\n]*(?:{_STRONG_KEYS})[^\S\r\n]*\r?\n[^\S\r\n]*['\"]?)(?:{_STRONG_VALUE})",
        rf"(\b(?:[A-Za-z0-9]+[-_])?(?:{_WEAKER_KEYS})['\"]?\s*[:=]\s*['\"]?)(?:{_WEAKER_VALUE})",
        rf"(\b(?:[A-Za-z0-9]+[-_])?(?:bearer|private[-_]?key|passphrase)['\"]?\s*[:=]\s*['\"]?)(?:{_STRONG_VALUE})",
    )
]
# env names, case-sensitive; `NAME = value` with spaces is code, so it counts only with a quoted literal; any other
# `*_KEY` (`SORT_KEY=created_at`, `CACHE_KEY=user:42`) needs the strong value shape, quoted or not
_ENV = r"((?<![\w.])[A-Z][A-Z0-9_]*_(?:{})=['\"]?)"
_ENV_RUN = r"""[^\s'"$<({`,;&\[]"""  # an index ends the run, and an indexed or called name is code (`argv[1]`)
_ENV_END = "(?!" + _ENV_RUN + ")"
_ENV_WHOLE = "[).!?]*" + _ENV_END  # the word is the whole value: `DEFAULT_AUTH=basic.`, not `basic-Xk29fLq83mZp`
# no value is a switch, a mode, a small number, a placeholder, a reference (`op://vault/item/field`) or a key file
_ENV_NOT_VALUE = (
    r"(?!(?:(?i:true|false|yes|no|on|off|null|none|nil|default|enabled|disabled|required|optional|basic|bearer|digest"
    r"|token|oauth2?|oidc|saml|ldap|trust|anonymous)|\d{1,4})" + _ENV_WHOLE + "|(?i:" + _user_pass(_ENV_WHOLE) + "))"
    r"(?![a-z][a-z\d+.-]*://[^\s'\"@=]*(?![^\s'\"])|\.{1,2}/|"
    + _SSH_KEY_FILE
    + r"|[^\s'\"=]*\.(?i:pem|key|json|p12|pfx|crt|der|jks|ppk|gpg|asc)(?![^\s'\"]))"
)
_REDACT += [
    re.compile(
        _ENV.format("PASS|PASSWD|PASSPHRASE|PWD|AUTH|BEARER|CREDS?|PRIVATE_KEY|SECRET_KEY|API_KEY|ACCESS_KEY")
        + _ENV_NOT_VALUE
        + r"(?!(?:(?<=AUTH=)|(?<=AUTH=['\"]))"  # an auth mode of several words: `SPRING_AUTH=oauth2-client`
        + _name("+", _ENV_END)
        + r")[^\s'\"$<({`,;&/~%*\[]"
        + _ENV_RUN
        + "{3,}"
        + _ENV_END
        + r"(?![(\[])"
    ),
    re.compile(_ENV.format("KEY") + "(?:" + _strong(quoted=False, names=_name("strong", _RUN_END)) + ")"),
    re.compile(
        r"((?<![\w.])[A-Z][A-Z0-9_]*_(?:PASS|PASSWD|PWD)\s+=\s+(['\"]))(?!"
        + _name("+", r"(?=\2)")
        + r")[^\s'\"]{4,}(?=\2)"
    ),
]

_FLAG_RX = next(rx for rx in _REDACT if rx.pattern == _CREDENTIAL_FLAG)
_REDACT.append(re.compile("()(?:" + _EXTRA_TOKENS + ")"))
_POSITIONAL_SECRETS = re.compile(
    r"(\b(?:sshpass\b[^\n;&|]*?\s-p\s+|docker\s+login\b[^\n;&|]*?\s-p\s+"
    r"|redis-cli\b[^\n;&|]*?\s-a\s+|vault\s+login\s+(?!['\"]?-)|gh\s+secret\s+set\b[^\n;&|]*?\s(?:--body|-b)(?:=|\s+)"
    r"|npm\s+config\s+set\s+[^\s]+:_authToken\s+)['\"]?)(?![$<{\[]|\[redacted\])[^\s'\";|&]+",
    re.I,
)
_REDACT.append(_POSITIONAL_SECRETS)
_PATTERNS.append(("a secret-shaped value", _POSITIONAL_SECRETS))
_COMMAND_TOKEN = re.compile(r"[^\s'\"`=;<>]+")
_CREDENTIAL_ARGUMENT = re.compile(
    r"((?<![\w.-])(?:[A-Za-z0-9]+[-_.]){1,3}"
    r"(?:password|passwd|secret|api[-_]?key|access[-_]?key|credential|private[-_]?key|pass|token)\s+['\"]?)"
    r"[^\s'\";|&]+",
    re.I,
)
_SECRET_PRINT = re.compile(
    r"\bprintenv\s+(?:--\s+)?(['\"]?[A-Za-z_]\w*['\"]?(?:[ \t]+['\"]?[A-Za-z_]\w*['\"]?)*)"
    r"|\becho\s+(?:-n\s+)?['\"]?\$\{?([A-Za-z_]\w*)"
)
_SECRET_NAME = re.compile(r"token|secret|pass|credential|api_?key|access_?key|private_?key|auth|bearer", re.I)


def command_redact(text: str) -> str:
    """Mask opaque values, including hashes, while retaining structured command arguments."""
    text = redact(text)
    text = _CREDENTIAL_ARGUMENT.sub(lambda m: m[1] + "[redacted]", text)
    return _COMMAND_TOKEN.sub(
        lambda m: (
            "[redacted]"
            if len(m[0]) >= 20
            and re.search(r"[A-Za-z]", m[0])
            and re.search(r"\d", m[0])
            and not re.search(r"[/\\]|::|\.[A-Za-z][A-Za-z0-9]*$", m[0])
            else m[0]
        ),
        text,
    )


def secret_output(command: str) -> bool:
    return any(
        _SECRET_NAME.search(name)
        for match in _SECRET_PRINT.finditer(command)
        for name in re.findall(r"[A-Za-z_]\w*", match[1] or match[2])
    )


def without_heredoc(command: str) -> str:
    """Keep heredoc headers and later commands, but never their bodies in command evidence."""
    if "\n" not in command and "[heredoc withheld]" in command:
        return command
    lines, kept, pending = command.splitlines(keepends=True), [], []
    for line in lines:
        if pending:
            delimiter, tabs = pending[0]
            end = line.rstrip("\r\n")
            if (end.lstrip("\t") if tabs else end) == delimiter:
                pending.pop(0)
            continue
        kept.append(line)
        operators = re.findall(r"(?<!<)<<(?!<)", line)
        pending = [
            (m[2] or m[3] or m[4], bool(m[1]))
            for m in re.finditer(
                r"(?<!<)<<(-?)(?!<)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|([^\s'\";|&<>\\]+))(?=[\s;|&]|$)",
                line,
            )
        ]
        if len(operators) != len(pending):
            pending = [(None, False)]  # Unrecognized delimiters withhold the rest, never guess at a closing line.
        if pending:
            kept.append("[heredoc withheld]\n")
    return "".join(kept)


_ARTICLE = r"(?i:the|this|that|an?)"
_THEN_WORD = re.compile(r"\s+[A-Za-z]+(?:[\s.,;!?)]|$)")
_THEN_ARTICLE = re.compile(rf"\s+{_ARTICLE}\s+[A-Za-z]+\b")
_HANDLING = r"(?:touch|commit|log|print|share|expose|store|leak|paste|hard-?code|rotate)"
_HANDLING_ING = (
    r"(?:touching|committing|logging|printing|sharing|exposing|storing|leaking|pasting|hard-?coding|rotating)"
)
_DETERMINER = r"(?:\s+(?:the|your|our|their|my|any|all|these|those))?"
# an instruction about credentials, not a label for them: a prohibition then a handling verb (`never log`, `don't
# touch`, `avoid storing`), or an imperative opening its clause (`Rotate credentials:`); a noun still labels
# (`Online store credentials:`)
_PROHIBITED = re.compile(
    rf"\b(?:don['’]?t|do\s+not|never|avoid|stop|no\s+need\s+to)\s+(?:{_HANDLING}|{_HANDLING_ING}){_DETERMINER}$", re.I
)
_IMPERATIVE_HANDLING = re.compile(rf"(?:please\s+)?{_HANDLING}{_DETERMINER}", re.I)


def _prose(m: re.Match) -> bool:
    """`credentials: the message` in a sentence, or `credentials` in an instruction (`never log credentials: use the
    vault`), is prose, not an assignment."""
    clause = re.fullmatch(r"(?i:credentials?\s*:\s+)([A-Za-z][a-z]*)", m[0])
    if not clause:
        return False
    window = m.string[max(0, m.start() - C.PROSE_CLAUSE_CHARS) : m.start()]  # never the whole line: linear time
    line = window[window.rfind("\n") + 1 :]
    whole = len(line) < len(window) or m.start() <= C.PROSE_CLAUSE_CHARS  # the window reaches the line start
    prefix = line.rstrip()
    if not prefix or (whole and re.fullmatch(r"\s*(?:[-*+]|\d+[.)])?", prefix)) or prefix[-1] in "\"'`{[(,:=":
        return False  # a line start, list marker or quote begins an assignment (`- credentials: the ...`)
    if re.fullmatch(_ARTICLE, clause[1]):  # an article then a plain word, not the rest of a value (`a&LongSecret42!`)
        return bool(_THEN_WORD.match(m.string, m.end()))
    if not _THEN_ARTICLE.match(m.string, m.end()):
        return False  # a word opens a clause only before an article: `fix the tree`, not `correcthorse set`
    cut = max(prefix.rfind(c) for c in ".!?;:")
    opening = re.sub(r"^\s*(?:[-*+]\s|\d+[.)]\s)?\s*", "", prefix[cut + 1 :]) if cut >= 0 or whole else None
    return bool(_PROHIBITED.search(prefix) or (opening is not None and _IMPERATIVE_HANDLING.fullmatch(opening)))


def _keep_match(m: re.Match) -> bool:
    return m.lastgroup == "plain_uri" or _prose(m)


def redact(text: str) -> str:
    """Secret-looking values in text nowitknows keeps from a session (tool calls in digests and the search index)."""
    for rx in _REDACT:
        text = rx.sub(lambda m: m[0] if _keep_match(m) else m.group(1) + "[redacted]", text)
    return text


def secret_shaped(text: str) -> bool:
    """Whether text holds a value redact would replace; the gate for keeping a reply or delivering a body."""
    return any(
        match[0] != match[1] + "[redacted]" and not _keep_match(match) for rx in _REDACT for match in rx.finditer(text)
    )


def scan_change(before: str | None, after: str) -> str | None:
    """Scan what a write adds: a match counts when it touches an added line, so a construct split across lines
    (a backslash continuation, a pipe finished by a later patch) is caught, and one already there never locks a file."""
    a = _fold(after)
    if before is None:
        return scan(a)
    from bisect import bisect_left

    had, spans, start = set(lines(_fold(before))), [], 0
    for m in re.finditer(r"\r?\n|\Z", a):
        if a[start : m.start()] not in had:
            spans.append((start, m.start()))
        if not m.group():
            break
        start = m.end()
    # Each next start follows the previous delimiter, so both span boundaries increase.
    ends = [hi for _, hi in spans]
    for label, rx in _PATTERNS:
        if any((i := bisect_left(ends, x.start())) < len(spans) and spans[i][0] <= x.end() for x in rx.finditer(a)):
            return f"blocked: contains {label}"
    return None


def _fold(text: str) -> str:
    return re.sub(r"\\\r?\n", " ", text or "")  # a shell reads `cmd \<newline> | sh` as one line


def lines(text: str) -> list[str]:
    """Split on real line breaks only: str.splitlines also splits on U+2028, form feeds and the like, which would
    cut `curl URL<U+2028>| sh` into two harmless-looking lines."""
    return re.split(r"\r?\n", text)


def scan(text: str) -> str | None:
    for label, rx in _PATTERNS:
        if rx.search(text or ""):
            return f"blocked: contains {label}"
    return None


def imperative(text: str) -> bool:
    return bool(_IMPERATIVE.search(text or ""))
