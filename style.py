"""
Text styling helpers.

- fancy(text)  -> converts normal text into a "𝐇ʟᴏ" style bold-small-caps
  unicode font (uppercase letters -> mathematical bold capitals,
  lowercase letters -> small-caps unicode letters, digits -> bold digits).
  This is just Unicode character substitution, not a real font, but it's
  the closest thing Telegram supports (there's no native "font" setting).

- blockquote(text) -> wraps text in Telegram's native <blockquote> HTML tag
  (supported since Bot API 7.0). NOTE: a <blockquote> cannot contain a
  <pre>/<code> block (Telegram forbids nesting those), so JSON / code
  output must always be sent as its own separate message, outside of
  any blockquote.
"""

# Uppercase A-Z -> Mathematical Bold Capital (U+1D400 - U+1D419)
_UPPER = {chr(65 + i): chr(0x1D400 + i) for i in range(26)}

# Lowercase a-z -> small caps unicode letters (closest available glyphs;
# 's' and 'x' have no dedicated small-caps codepoint in common fonts so a
# couple of visually-similar fallbacks are used).
_LOWER = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ",
    "g": "ɢ", "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ",
    "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ",
    "s": "ꜱ", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ",
}

# Digits 0-9 -> Mathematical Bold Digit (U+1D7CE - U+1D7D7)
_DIGIT = {str(i): chr(0x1D7CE + i) for i in range(10)}


def fancy(text: str) -> str:
    """Convert plain text to the bold-small-caps fancy font."""
    out = []
    for ch in text:
        if ch in _UPPER:
            out.append(_UPPER[ch])
        elif ch in _LOWER:
            out.append(_LOWER[ch])
        elif ch in _DIGIT:
            out.append(_DIGIT[ch])
        else:
            out.append(ch)
    return "".join(out)


def blockquote(text: str) -> str:
    """Wrap text in a native Telegram blockquote (HTML parse mode)."""
    return f"<blockquote>{text}</blockquote>"


def styled(text: str) -> str:
    """fancy() + blockquote() combined — the standard style for bot replies."""
    return blockquote(fancy(text))
