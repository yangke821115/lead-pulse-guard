"""知识库加载。

设计要点：默认关闭。如果「已确认事实」是空的，模型被明确告知
不得陈述任何产品信息 —— 它只能回「可以提供详细资料」。
"""
import os, re

from . import config

COMMENT_BLOCK = re.compile(r"<!--.*?-->", re.S)
HEADING = re.compile(r"^#.*$", re.M)


def _subdir(slug):
    """按主页分目录存放知识库。

    slug 来自事件里的 page.kb（由 gateway 打标），例如
    <slug-a> / <slug-b>，slug 由 gateway 按主页打标。
    找不到该目录时回退到 KB_DIR 根目录。
    """
    if slug:
        safe = re.sub(r"[^a-z0-9_-]", "", slug.lower())
        path = os.path.join(config.KB_DIR, safe)
        if os.path.isdir(path):
            return path
    return config.KB_DIR


def _read(name, base=None):
    path = os.path.join(base or config.KB_DIR, name)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _substantive(text):
    """只把「- 」或「* 」开头的列表项当作真实条目。

    标题、引用块、说明文字一律不算。这一条很关键：知识库文件里
    带有解释性的占位文字（例如「> 当前为空」），如果被误当成事实，
    就会让「事实库为空」的判断失效，从而抵消掉默认关闭的保护。
    """
    text = COMMENT_BLOCK.sub("", text)
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
        elif stripped.startswith("* "):
            items.append(stripped[2:].strip())
    return [i for i in items if i]


def load(slug=None):
    base = _subdir(slug)
    facts_raw = _read("confirmed-facts.md", base)
    forbid_raw = _read("do-not-state.md", base)
    style_raw = _read("style-notes.md", base)

    facts = _substantive(facts_raw)
    forbid = _substantive(forbid_raw)
    style = _substantive(style_raw)

    return {
        "facts": facts,
        "forbid": forbid,
        "style": style,
        "facts_empty": not facts,
    }


def as_prompt_block(kb):
    """把知识库渲染成给模型看的文本。空的事实库要显式标成空的。"""
    if kb["facts_empty"]:
        facts_text = (
            "（本栏为空。你目前【没有任何】经过确认的产品信息，\n"
            "因此不得对产品作任何具体陈述 —— 包括不限于：规格、认证、\n"
            "保修期、产地、性能、价格、交期。只能引导对方索取详细资料。）"
        )
    else:
        facts_text = "\n".join("- " + f for f in kb["facts"])

    forbid_text = ("\n".join("- " + f for f in kb["forbid"])
                   if kb["forbid"] else "（无额外条目）")
    style_text = ("\n".join("- " + s for s in kb["style"])
                  if kb["style"] else "（无额外条目）")

    return (
        "## 知识库 · 已确认事实（唯一可引用来源）\n%s\n\n"
        "## 知识库 · 禁止陈述\n%s\n\n"
        "## 知识库 · 语气偏好\n%s" % (facts_text, forbid_text, style_text)
    )
