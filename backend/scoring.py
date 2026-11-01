"""
scoring.py
CVSS-style severity scoring for intercepted prompts, following the
SafeScholar "CVSS Scoring Guidelines" page.

    score = max(matched base scores)
    if more than one category matched:  score += (matched - 1) * 0.4
    if academic phrasing detected:      score -= 2.5
    score = clamp(score, 0.0, 10.0), rounded to 1 decimal

Severity buckets:  Critical 9.0-10.0 | High 7.0-8.9 | Medium 4.0-6.9
                   Low 0.1-3.9 | None 0.0 (no incident recorded)
Default policy:    block at >= 7.0, warn at 4.0-6.9, log-and-allow below that.

Every result carries a step-by-step explanation, so the dashboard can show
exactly WHY a prompt got its score. The pattern lists below are a starter set:
add or edit patterns to tune detection, the scoring maths doesn't need to change.
"""

import re

BOOST_PER_EXTRA_CATEGORY = 0.4
WHITELIST_REDUCTION = 2.5
BLOCK_THRESHOLD = 7.0
WARN_THRESHOLD = 4.0
MAX_EVIDENCE_PER_CATEGORY = 3

# key -> label, base score, regex patterns (all case-insensitive)
CATEGORIES = {
    "prompt_injection": {
        "label": "LLM01: Prompt Injection / Jailbreak",
        "base": 7.8,
        "patterns": [
            r"\b(?:ignore|disregard|forget|override)\b.{0,30}\b(?:previous|prior|above|earlier|all)\b.{0,20}\b(?:instructions?|prompts?|rules|guidelines)\b",
            r"\bDAN\s+mode\b",
            r"\bdo anything now\b",
            r"\b(?:bypass|disable|circumvent|turn off)\b.{0,25}\b(?:safety|content|ethical)\b.{0,15}\b(?:rules|filters?|guidelines|restrictions|policies)\b",
            r"\bdeveloper mode\b",
            r"\b(?:reveal|show|print)\b.{0,20}\bsystem prompt\b",
            r"\bjailbreak\b",
            r"\byou (?:are|have) no (?:restrictions|rules|limits|filters)\b",
        ],
    },
    "harmful_content": {
        "label": "LLM02: Harmful Content / Cyber Misuse",
        "base": 9.0,
        "patterns": [
            r"\b(?:make|build|create|synthesi[sz]e|assemble)\b.{0,30}\b(?:bomb|explosives?|nerve agent|chemical weapon|bioweapon)\b",
            r"\bsynthesi[sz]\w*\b.{0,30}\b(?:weapon|explosive|toxin|poison)\w*",
            r"\b(?:write|create|code|build|develop)\b.{0,30}\b(?:malware|ransomware|keylogger|virus|trojan|botnet)\b",
            r"\b(?:steal|harvest|dump|phish)\w*\b.{0,30}\b(?:credentials?|passwords?|login details)\b",
            r"\b(?:hack|hack into|break into|exploit)\b.{0,40}\b(?:websites?|sites?|systems?|servers?|networks?|databases?)\b",
        ],
    },
    "sensitive_information": {
    "label": "LLM03: Sensitive Information Disclosure",
    "base": 7.8,
    "patterns": [
        r"\b(?:reveal|show|print|expose|leak)\b.{0,30}\b(?:api keys?|secret keys?|access tokens?|private keys?)\b",
        r"\b(?:reveal|show|dump|extract)\b.{0,30}\b(?:passwords?|credentials?|private records?|confidential data)\b",
        r"\b(?:steal|extract|exfiltrate)\b.{0,30}\b(?:personal data|student records|user data)\b",
        ],
    },
    "academic_dishonesty": {
        "label": "LLM04: Academic Dishonesty",
        "base": 5.4,
        "patterns": [
            r"\bgive me (?:the )?(?:exam |test |quiz |assignment )?answers?\b",
            r"\bwrite my (?:essay|assignment|thesis|report|paper|dissertation)\b",
            r"\bdo my (?:homework|assignment|coursework|essay|project)\b",
            r"\bsolve this (?:exam|test|quiz)(?: question)?\b",
            r"\b(?:complete|finish) my (?:assignment|homework|coursework)\b",
        ],
    },
    "social_engineering": {
        "label": "LLM05: AI Manipulation / Roleplay Bypass",
        "base": 6.6,    
        "patterns": [
            r"\bpretend (?:that )?(?:you are|you're|to be)\b.{0,20}\b(?:my )?(?:professor|lecturer|teacher|admin\w*|developer)\b",
            r"\bI(?: am|'m) (?:the |your |an? )?(?:administrator|admin|developer|owner)\b",
            r"\bemergency (?:bypass|override|access)\b",
            r"\b(?:act|respond) as (?:my |the )?(?:professor|administrator|system admin)\b",
        ],
    },
    "encoded_attacks": {
    "label": "LLM06: Encoded / Obfuscated Attacks",
    "base": 6.4,
    "patterns": [
        r"\b(?:decode|decrypt|interpret)\b.{0,40}\b(?:base64|hexadecimal|rot13)\b",
        r"\b(?:bypass|evade|avoid)\b.{0,40}\b(?:detection|moderation|content filters?|safety filters?)\b",
        r"\b(?:hide|conceal|obfuscate)\b.{0,40}\b(?:instructions?|prompts?|commands?)\b",
        ],
    },
}

# Legitimate academic phrasing: reduces the score by 2.5 when present.
WHITELIST_PATTERNS = [
    r"\bexplain the concept of\b",
    r"\bfor my research on\b",
    r"\bfor (?:academic|educational|research) purposes\b",
    r"\bfor (?:a|my) (?:paper|thesis|dissertation|study|class) on\b",
    r"\bhow do researchers\b",
]

_COMPILED = {
    key: [re.compile(p, re.IGNORECASE) for p in cat["patterns"]]
    for key, cat in CATEGORIES.items()
}
_WHITELIST = [re.compile(p, re.IGNORECASE) for p in WHITELIST_PATTERNS]


def severity_for(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score >= 0.1:
        return "low"
    return "none"


def action_for(score: float) -> str:
    if score >= BLOCK_THRESHOLD:
        return "block"
    if score >= WARN_THRESHOLD:
        return "warn"
    if score >= 0.1:
        return "allow"  # logged but allowed
    return "none"


def _snippet(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _find_evidence(patterns, text):
    found = []
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            snippet = _snippet(match.group(0))
            if snippet not in found:
                found.append(snippet)
        if len(found) >= MAX_EVIDENCE_PER_CATEGORY:
            break
    return found


def score_prompt(prompt: str) -> dict:
    """Score one prompt and explain the result. Safe to call on any text."""
    matched = []
    for key, cat in CATEGORIES.items():
        evidence = _find_evidence(_COMPILED[key], prompt)
        if evidence:
            matched.append(
                {"key": key, "label": cat["label"], "base": cat["base"], "evidence": evidence}
            )

    whitelist_hits = _find_evidence(_WHITELIST, prompt)

    # Nothing matched: no incident, whatever the phrasing.
    if not matched:
        return {
            "is_incident": False,
            "score": 0.0,
            "severity": "none",
            "action": "none",
            "primary_category": None,
            "categories": [],
            "base_score": 0.0,
            "multi_category_boost": 0.0,
            "whitelist_hits": whitelist_hits,
            "whitelist_reduction": 0.0,
            "steps": ["No threat patterns matched, so no incident was recorded (score 0.0)."],
            "summary": "No threat patterns matched.",
        }

    primary = max(matched, key=lambda m: m["base"])
    base_score = primary["base"]
    boost = round((len(matched) - 1) * BOOST_PER_EXTRA_CATEGORY, 1)
    reduction = WHITELIST_REDUCTION if whitelist_hits else 0.0

    raw = base_score + boost - reduction
    score = round(min(max(raw, 0.0), 10.0), 1)
    severity = severity_for(score)
    action = action_for(score)

    steps = []
    for m in matched:
        quoted = ", ".join(f"“{e}”" for e in m["evidence"])
        steps.append(f"Matched {m['label']} (base {m['base']:.1f}): {quoted}")
    steps.append(f"Highest base score is {base_score:.1f} ({primary['label']}).")
    if boost:
        extra = len(matched) - 1
        plural = "category" if extra == 1 else "categories"
        steps.append(f"Multi-category boost: {extra} additional {plural} × 0.4 = +{boost:.1f}")
    if reduction:
        quoted = ", ".join(f"“{e}”" for e in whitelist_hits)
        steps.append(f"Academic phrasing detected ({quoted}): −{reduction:.1f}")
    if raw != score:
        steps.append(f"Score clamped to the 0.0–10.0 range: {raw:.1f} → {score:.1f}")
    steps.append(f"Final score {score:.1f} → {severity.capitalize()}.")

    policy = {
        "block": "Blocked by the default policy (score of 7.0 or more).",
        "warn": "The user is warned by default (Medium).",
        "allow": "Logged but allowed (Low).",
    }[action]
    steps.append(policy)

    summary = f"{primary['label']} detected: “{primary['evidence'][0]}”"
    return {
        "is_incident": True,
        "score": score,
        "severity": severity,
        "action": action,
        "primary_category": primary["key"],
        "categories": matched,
        "base_score": base_score,
        "multi_category_boost": boost,
        "whitelist_hits": whitelist_hits,
        "whitelist_reduction": reduction,
        "steps": steps,
        "summary": summary,
    }
