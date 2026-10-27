"""
DSML — DeepSeek Markup Language 工具调用格式解析器

格式：
  <|DSML|tool_calls>
    <|DSML|invoke name="TOOL_NAME">
      <|DSML|parameter name="ARG_NAME"><![CDATA[VALUE]]></|DSML|parameter>
    </|DSML|invoke>
  </|DSML|tool_calls>
"""
import json
import re
import uuid
from typing import Any

_CDATA_OPEN = "<![CDATA["
_CDATA_CLOSE = "]]>"

# --- Parser longgar (v3.3.20, disejajarkan dengan dsh-plugin/src/dsml.js) ---
# Model (dan prompt pihak ketiga) menghasilkan varian tag: pipa fullwidth ｜,
# pipa ASCII |, pipa hilang, spasi di dalam tag, wrapper bernama function_calls
# atau calls, serta penutup parameter tanpa '<' ("nilai｜DSML｜parameter>").
# Semua varian dilipat ke bentuk kanonik '<|DSML|name …>' sebelum diparse.
_PIPE = r"[|\uff5c]"
_TAG_NAMES = r"tool_calls|tool_call|function_calls|calls|invoke|parameter"
_LOOSE_TAG_RE = re.compile(
    r"<\s*(/?)\s*" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(" + _TAG_NAMES + r")([^>]*)>",
    re.IGNORECASE,
)
_BARE_CLOSE_RE = re.compile(
    r"(?<![</|\uff5c])/?\s*" + _PIPE + r"{1,2}\s*DSML\s*" + _PIPE + r"{0,2}\s*(" + _TAG_NAMES + r")\s*>",
    re.IGNORECASE,
)
_TRUNC_TAG_RE = re.compile(
    r"(?:</?\s*" + _PIPE + r"{0,2}\s*DSML\s*" + _PIPE + r"{0,2}|(?<![</|\uff5c])/?\s*" + _PIPE + r"{1,2}\s*DSML\s*" + _PIPE + r"{0,2})"
    r"\s*(?:" + _TAG_NAMES + r")(?:[^>\"']|\"[^\"]*\"|'[^']*')*>?",
    re.IGNORECASE,
)


def _canonical_name(name: str) -> str:
    low = name.lower()
    if low in ("function_calls", "calls"):
        return "tool_calls"
    return low


def normalize_dsml(text: str) -> str:
    """Lipat semua varian tag DSML ke bentuk kanonik '<|DSML|name ...>'.

    Alias wrapper function_calls/calls menjadi tool_calls; pipa apa pun
    (｜, |, atau tanpa pipa) dinormalisasi; spasi di dalam tag dibuang;
    penutup tanpa '<' diberi bentuk lengkapnya. Idempoten.
    """
    if not text or "dsml" not in text.lower():
        return text
    text = _BARE_CLOSE_RE.sub(lambda m: "</|DSML|" + _canonical_name(m.group(1)) + ">", text)
    text = _LOOSE_TAG_RE.sub(
        lambda m: ("<" if not m.group(1) else "</") + "|DSML|" + _canonical_name(m.group(2)) + m.group(3) + ">",
        text,
    )
    return text




def strip_dsml_markup(text: str) -> str:
    """去除 DSML 前缀，保留原始 XML 结构。"""
    if not text:
        return text
    text = normalize_dsml(text)
    parts = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text[i:].startswith(_CDATA_OPEN):
            close = text.find(_CDATA_CLOSE, i + len(_CDATA_OPEN))
            if close == -1:
                parts.append(text[i:])
                break
            parts.append(text[i:close + len(_CDATA_CLOSE)])
            i = close + len(_CDATA_CLOSE)
            continue
        if c != '<':
            parts.append(c)
            i += 1
            continue
        end = text.find('>', i)
        if end == -1:
            parts.append(text[i:])
            break
        inner = text[i + 1:end]
        rest = inner[1:] if inner.startswith('/') else inner
        # Check for |DSML| prefix (with or without leading <)
        j = 0
        dsml = False
        while j < len(rest):
            ch = rest[j]
            if ch in ('|', '\uff5c', ' ', '\t', '\r', '\n'):
                j += 1
                if ch == '|':
                    dsml = True
            elif rest[j:j+4].lower() == 'dsml':
                j += 4
                dsml = True
            else:
                break
        if dsml:
            name_end = j
            while name_end < len(rest) and (rest[name_end].isalnum() or rest[name_end] == '_'):
                name_end += 1
            tag_name = rest[j:name_end].lower()
            if tag_name in ("tool_calls", "tool_call", "invoke", "parameter"):
                prefix = '</' if inner.startswith('/') else '<'
                parts.append(prefix + rest[j:] + '>')
                i = end + 1
                continue
        parts.append(text[i:end + 1])
        i = end + 1
    return ''.join(parts)


def extract_cdata(text: str) -> str:
    text = text.strip()
    if text.startswith(_CDATA_OPEN) and text.endswith(_CDATA_CLOSE):
        inner = text[len(_CDATA_OPEN):-len(_CDATA_CLOSE)]
        return inner
    return text


def parse_dsml_tool_calls(text: str, tool_names: list[str] | None = None) -> tuple[list[dict], str]:
    """从文本中解析 DSML 工具调用，返回 (tool_calls, cleaned_text)"""
    if not text:
        return [], text

    text = normalize_dsml(text)
    normalized = strip_dsml_markup(text)
    tool_calls = []

    # <tool_calls>...</tool_calls> 或 <tool_call>...</tool_call>
    for pattern in (
        r"<tool_calls>(.*?)</tool_calls>",
        r"<tool_call>(.*?)</tool_call>",
        r"<function_calls>(.*?)</function_calls>",
        r"<calls>(.*?)</calls>",
    ):
        blocks = re.findall(pattern, normalized, re.DOTALL | re.IGNORECASE)
        if blocks:
            break

    if not blocks:
        # 裸 <invoke> 无外层 wrapper
        invoke_bare = re.findall(
            r"<invoke\s+name=[\"']([^\"']+)[\"']>(.*?)</invoke>",
            normalized, re.DOTALL | re.IGNORECASE
        )
        for name, inner in invoke_bare:
            tc = _format_tool_call(name.strip(), _parse_parameters(inner))
            if tc:
                tool_calls.append(tc)

    for block_text in blocks:
        for name, inner in re.findall(
            r"<invoke\s+name=[\"']([^\"']+)[\"']>(.*?)</invoke>",
            block_text, re.DOTALL | re.IGNORECASE
        ):
            tc = _format_tool_call(name.strip(), _parse_parameters(inner))
            if tc:
                tool_calls.append(tc)

    cleaned = _clean_dsml_text(normalized)
    return tool_calls, cleaned


def _parse_parameters(inner_text: str) -> dict:
    args = {}
    for m in re.finditer(
        r"<parameter\s+name=[\"']([^\"']+)[\"'](?:\s+[a-zA-Z_][A-Za-z0-9_-]*=\"[^\"]*\")*>(.*?)</parameter>",
        inner_text, re.DOTALL | re.IGNORECASE
    ):
        key = m.group(1).strip()
        val_raw = m.group(2).strip()
        val_raw = extract_cdata(val_raw)
        try:
            val = json.loads(val_raw)
        except (json.JSONDecodeError, ValueError):
            val = _auto_type(val_raw)
        args[key] = val
    return args


def _auto_type(val: str) -> Any:
    if val.lower() in ("true",): return True
    if val.lower() in ("false",): return False
    if val.lower() in ("null", "none"): return None
    try: return int(val)
    except ValueError: pass
    try: return float(val)
    except ValueError: pass
    return val


def _format_tool_call(name: str, args: dict) -> dict | None:
    if not name:
        return None
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _clean_dsml_text(text: str) -> str:
    text = re.sub(
        r"<(?:tool_calls|tool_call|function_calls|calls)>.*?</(?:tool_calls|tool_call|function_calls|calls)>",
        "", text, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<invoke[^>]*>.*?</invoke>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<parameter[^>]*>.*?</parameter>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = _TRUNC_TAG_RE.sub("", text)
    text = re.sub(r"<!\[CDATA\[.*?\]\]>", "", text, flags=re.DOTALL)
    text = re.sub(r"\[citation:\d+\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def format_tool_calls_for_prompt(tool_calls_raw: Any) -> str:
    """将 OpenAI tool_calls 格式化为 DSML 提示词格式。"""
    if isinstance(tool_calls_raw, str):
        try:
            tool_calls_raw = json.loads(tool_calls_raw)
        except (json.JSONDecodeError, ValueError):
            return ""
    if not isinstance(tool_calls_raw, list) or not tool_calls_raw:
        return ""

    blocks = []
    for tc in tool_calls_raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {})
        name = tc.get("name") or fn.get("name", "")
        if not name:
            continue
        args = tc.get("arguments") or tc.get("input") or fn.get("arguments") or "{}"
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, ValueError):
                pass
        params = _format_params_dsml(args)
        block = f'  <|DSML|invoke name="{_escape_xml_attr(name)}">'
        if params.strip():
            block += "\n" + params + "\n  </|DSML|invoke>"
        else:
            block += "</|DSML|invoke>"
        blocks.append(block)

    if not blocks:
        return ""
    return "<|DSML|tool_calls>\n" + "\n".join(blocks) + "\n</|DSML|tool_calls>"


def _format_params_dsml(args: Any, indent: str = "    ") -> str:
    if isinstance(args, dict):
        if not args:
            return ""
        return "\n".join(_format_param_node(k, v, indent) for k, v in sorted(args.items()))
    elif isinstance(args, list):
        return "\n".join(_format_param_node("item", item, indent) for item in args)
    elif isinstance(args, str):
        return f'{indent}<|DSML|parameter name="content">{_cdata(args)}</|DSML|parameter>'
    else:
        return f'{indent}<|DSML|parameter name="value">{str(args)}</|DSML|parameter>'


def _format_param_node(name: str, value: Any, indent: str) -> str:
    open_tag = f'<|DSML|parameter name="{_escape_xml_attr(name)}">'
    close = "</|DSML|parameter>"
    if value is None:
        return f"{indent}{open_tag}{close}"
    if isinstance(value, (dict, list)):
        serialized = json.dumps(value, ensure_ascii=False)
        return f"{indent}{open_tag}{_cdata(serialized)}{close}"
    if isinstance(value, (bool, int, float)):
        return f"{indent}{open_tag}{str(value)}{close}"
    return f"{indent}{open_tag}{_cdata(str(value))}{close}"


def _cdata(text: str) -> str:
    if "]]>" in text:
        text = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


def _escape_xml_attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def build_dsml_tool_prompt(tools: list[dict], tool_choice: str | dict | None = None) -> str:
    """构建工具调用提示词，附加到 system message。"""
    if not tools:
        return ""

    choice_instruction = ""
    if tool_choice is not None:
        if tool_choice == "none":
            return ""
        elif tool_choice == "required":
            choice_instruction = (
                "You MUST call at least one of the available tools. "
                "Do not answer the user directly — always use a tool call.\n\n"
            )
        elif isinstance(tool_choice, dict):
            fn_name = tool_choice.get("function", {}).get("name", "")
            if fn_name:
                choice_instruction = (
                    f"You MUST call the tool '{fn_name}'. "
                    f"Do not call any other tool and do not answer the user directly.\n\n"
                )
        elif isinstance(tool_choice, str) and tool_choice not in ("auto", "required", "none"):
            choice_instruction = (
                f"You MUST call the tool '{tool_choice}'. "
                f"Do not call any other tool and do not answer the user directly.\n\n"
            )

    prompt = """You have access to tools. When you need to call a tool, respond with EXACTLY this format — no markdown fences, no extra text before or after:

<|DSML|tool_calls>
  <|DSML|invoke name="TOOL_NAME">
    <|DSML|parameter name="PARAMETER_NAME"><![CDATA[PARAMETER_VALUE]]></|DSML|parameter>
  </|DSML|invoke>
</|DSML|tool_calls>

RULES:
- Use <|DSML|tool_calls> wrapper with one or more <|DSML|invoke> entries
- Tool name in invoke name attribute
- String values MUST use <![CDATA[...]]>
- Numbers, booleans, null stay plain text
- First non-whitespace character must be < for tool calls
- NO explanations, NO markdown fences, NO extra text

""" + choice_instruction + "Available tools:\n"
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name") or tool.get("name", "")
        desc = (fn.get("description") or tool.get("description", "")).split("\n")[0].strip()
        prompt += f"  - {name}: {desc}\n" if desc and name else f"  - {name}\n"
    return prompt.strip()
