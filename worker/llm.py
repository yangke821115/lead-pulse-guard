"""DeepSeek 调用：把一条评论变成结构化的回复建议。

核心安全设计：系统提示词里把「知识库是唯一事实来源」写成硬规则，
并且在事实库为空时明确要求模型不得陈述任何产品信息。
"""
import json, re, time, urllib.error, urllib.request

from . import config

SYSTEM_PROMPT = """你是 Facebook 主页的客服助理。根据用户评论和下面提供的知识库，生成一条可以直接发布的回复建议，并给出分析。

# 最重要的规则：知识库是唯一事实来源

- 你**只能**陈述知识库「已确认事实」里明确写出的内容。
- 该章节以外的任何产品信息 —— 规格、参数、型号、认证、标准编号、保修期、价格、交期、产地、性能对比 —— **一律不得陈述**，即使你认为它「应该是对的」或「符合行业惯例」。
- 用户问到这些内容时，正确做法是：把问题记下来，回复「可以为您提供详细资料」，并引导对方点击主页的「发消息 / Message」按钮通过私信联系。**这不是回避，这是本主页的标准做法。**
- 「禁止陈述」章节列出的内容，任何情况下都不得出现在回复里。
- **如果「已确认事实」是空的，那么关于产品的任何具体说明都不得陈述**，只能用「可以提供详细资料」来回应。

# 回复调性（五条，必须全部满足）

1. 开场一句正面定调（感谢关注 / 表明该问题受重视）
2. **直接回应**用户问的那个具体问题 —— 能答的正面答；不能答的明确说「可以为您提供详细资料」，不含糊其辞，也不绕开去讲品牌
3. 不吹不夸 —— 不用最高级形容词，不做「行业领先」「第一」「顶级」类的无依据声明
4. 不编造 —— 见上
5. 收尾邀请进一步互动 —— **引导对方点击主页的「发消息 / Message」按钮私信联系我们**。不要写邮箱地址、电话或 WhatsApp；也不要请对方在评论区留联系方式。

# 长度（硬约束，优先级很高）

- 这是**社交媒体公开评论**，不是邮件，也不是问卷。
- **目标 250–400 字符，绝对不超过 450 字符。** 英文约 50–70 个词，中文约 150–250 字。
  （余量留给「please message us」这类必要的引导语，不要用满。）
- 固定三句结构：
  1. 一句正面定调（感谢关注 / 表明问题受重视）
  2. 一至两句直接回应被问的问题（能答就答；不能答就说明需转私信）
  3. 一句收尾引导（点击主页的「发消息」按钮私信）
- **禁止罗列清单、禁止逐条列出要客户提供的信息** —— 这是回复变长和被截断的头号原因。
  资格信息用**一句话带过**即可，例如「方便的话请告知材料、壁厚与工件尺寸」。
- 宁短勿长。客户在公开评论区读长文的概率为零。
- 超过 450 字符会被系统截断，客户会看到一句断掉的话 —— 必须自己控制住。

# 语言

回复语言必须跟随提问者：英文评论用英文回，中文评论用中文回。

# 关于「垃圾信息」

回答前先判断这条评论属于哪个业务分类，填入 domain：
@@DOMAIN_GUIDE@@
如果评论明显是垃圾广告、纯链接推广、与业务无关的内容，action 填「忽略」，reply 可以留空字符串。

# 输出格式

只输出 JSON，不要 markdown 代码块，不要任何解释文字：

{
  "domain": "@@DOMAIN_ENUM@@",
  "intent": "询价|技术咨询|售后|合作|垃圾信息|其他",
  "stage": "冷线索|温线索|热线索|非目标",
  "action": "回复|转销售|忽略",
  "language": "zh|en",
  "reply": "最终回复正文",
  "self_check": {
    "answers_question": true,
    "cites_only_kb": true,
    "invented_specifics": false,
    "note": ""
  }
}"""

# 把提示词里的分类占位符换成实际配置（模块加载时做一次，运行期零开销）
def _domain_enum():
    items = [d.strip() for d in config.DOMAIN_ENUM.split(",") if d.strip()]
    return "|".join(items) if items else "general|other"


def _domain_guide():
    if config.DOMAIN_GUIDE.strip():
        return config.DOMAIN_GUIDE.rstrip()
    return "\n".join("  · %s" % d.strip() for d in _domain_enum().split("|"))


SYSTEM_PROMPT = (SYSTEM_PROMPT
                 .replace("@@DOMAIN_ENUM@@", _domain_enum())
                 .replace("@@DOMAIN_GUIDE@@", _domain_guide()))


USER_TEMPLATE = """{kb}

# 待处理评论

- 主页：{page_name}（知识库主题：{kb_slug}）
- 评论者：{author_name}
- 评论内容：
{comment}"""


def _post(payload, url, keyname, timeout=None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Authorization", "Bearer " + config.read_secret(keyname))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout or config.LLM_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _attempt(event, kb_block, url, keyname, model, timeout=None, images=None):
    """单次尝试。返回 (suggestion, usage)；任何问题都抛异常。"""
    comment = event.get("comment") or {}
    user_msg = USER_TEMPLATE.format(
        kb=kb_block,
        page_name=(event.get("page") or {}).get("name") or "未知主页",
        kb_slug=(event.get("page") or {}).get("kb") or "无",
        author_name=(event.get("author") or {}).get("name") or "某位用户",
        comment=(comment.get("message") or "（无文字内容）"),
    )
    if images:
        content = [{"type": "text", "text": user_msg}]
        for uri in images:
            content.append({"type": "image_url", "image_url": {"url": uri}})
    else:
        content = user_msg
    body = _post({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": config.TEMPERATURE,
        "max_tokens": config.MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }, url, keyname, timeout)
    choice = body["choices"][0]
    content = choice["message"]["content"]
    finish = choice.get("finish_reason")
    usage = body.get("usage", {})

    # finish_reason == "length" 意味着输出被 max_tokens 硬截断 ——
    # JSON 必然未闭合，解析一定失败。必须显式告警，不能悄悄丢弃。
    if finish == "length":
        raise RuntimeError(
            "输出被 max_tokens=%s 截断（finish_reason=length），"
            "知识库或回复过长。原始内容尾部: %r"
            % (config.MAX_TOKENS, content[-120:]))

    if not (content or "").strip():
        raise RuntimeError(
            "模型返回空内容。finish_reason=%r usage=%r" % (finish, usage))

    # 宽容解析：有些模型会自作主张包上 markdown 代码块
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        suggestion = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(
            "JSON 解析失败: %s | finish_reason=%r | 内容头 200 字: %r | 尾部 120 字: %r"
            % (exc, finish, content[:200], content[-120:])) from exc

    # 兜底：模型漏字段时给出安全默认值
    suggestion.setdefault("domain", "其他")
    suggestion.setdefault("intent", "其他")
    suggestion.setdefault("stage", "冷线索")
    suggestion.setdefault("action", "回复")
    suggestion.setdefault("language", "en")
    suggestion.setdefault("reply", "")
    check = suggestion.setdefault("self_check", {})
    check.setdefault("answers_question", True)
    check.setdefault("cites_only_kb", True)
    check.setdefault("invented_specifics", False)
    check.setdefault("note", "")
    return suggestion, usage

# ---------- 识图模型 fallback chain（2026-10-16 实测）----------
# 30 个模型用同一张「4 色块 + 第 3 块藏白方块」的测试图逐个测，
# 两轮复测后确认能用的 12 个。按延迟从快到慢排，失败自动换下一个。
# 用法：llm.generate_viz(event, kb_block)  —— 与 generate() 同接口，只是模型链不同。
VIZ_FALLBACK = [
    "ministral-3b-latest",
    "ministral-3b-2512",
    "ministral-14b-2512",
    "gemini-3-flash-preview",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "openrouter/free",          # 路由伪模型，命中哪个不确定，实测不吐 JSON
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-robotics-er-2-preview",
    "gemma-4-26b-a4b-it",
]

# 已确认废弃的（两轮都失败，不再进链）：
#   stealth/space-bunny-alpha        上游 404，不是限流
#   @cf/llava-hf/llava-1.5-7b-hf     HTTP 400 请求格式不支持
#   @cf/meta/llama-3.2-11b-vision    429 限流
#   @cf/moondream / @cf/qwen         502 empty_completion
#   qwen/qwen3.8-27b                 413 请求太大
#   gemma-4-31b-it                   timeout
#   nvidia/nemotron-3-nano-omni      502 empty_completion
#   nvidia/nemotron-3.5-content-safety  内容安全分类器，返回 "User Safety: safe"
#   gemini-3.1/3.5/3.6/3.7/3.8-flash  502 upstream_error（系列级故障，非模型问题）
#   @cf/mistralai/mistral-small-3.1  颜色顺序答反，不可用
#   dots-studio/dots-3-note-preview  不输出 JSON

def generate_viz(event, kb_block, images=None):
    """识图场景的生成入口。与 generate() 同接口，只是走 VIZ_FALLBACK 链。

    为什么单独一条链：评论回复是纯文本 + 知识库约束，用 auto 路由没问题；
    识图需要输入里带 image_url，很多模型对这个格式支持不好，
    所以显式维护一份「已实测能用」的白名单，而不是依赖 auto 路由。
    """
    attempts = []
    for model in VIZ_FALLBACK:
        t0 = time.time()
        try:
            suggestion, usage = _attempt(event, kb_block,
                                         config.FREELLM_URL, "freellmapi-api-key",
                                         model, config.FREE_TIMEOUT, images=images)
        except Exception as exc:
            attempts.append({"via": "freellmapi", "model": model,
                             "seconds": round(time.time() - t0, 1),
                             "error": repr(exc)[:200]})
            continue
        meta = dict(usage or {})
        meta.update({"via": "freellmapi", "model": model,
                     "seconds": round(time.time() - t0, 1),
                     "fell_back": bool(attempts), "attempts": attempts})
        return suggestion, meta

    raise RuntimeError("识图链全部 %d 次失败: %s" % (len(VIZ_FALLBACK), attempts))


def generate(event, kb_block):
    """按「免费池优先、官方兜底」的顺序尝试，返回 (suggestion, meta)。

    故意不 fail-fast：单次失败就换下一个后端重试。
    meta 里带 attempts 明细，便于事后评估免费池质量。
    """
    plan = [("freellmapi", config.FREELLM_URL, "freellmapi-api-key",
             config.FREE_LLM_MODEL, config.FREE_TIMEOUT)
            for _ in range(1)]
    plan.append(("deepseek", config.DEEPSEEK_URL, "deepseek-api-key",
                 config.FALLBACK_MODEL, config.LLM_TIMEOUT))

    attempts = []
    for name, url, keyname, model, timeout in plan:
        t0 = time.time()
        try:
            suggestion, usage = _attempt(event, kb_block, url, keyname, model, timeout)
        except Exception as exc:
            attempts.append({"via": name, "model": model,
                             "seconds": round(time.time() - t0, 1),
                             "error": repr(exc)[:300]})
            continue
        meta = dict(usage or {})
        meta.update({"via": name, "model": model,
                     "seconds": round(time.time() - t0, 1),
                     "fell_back": bool(attempts), "attempts": attempts})
        return suggestion, meta

    raise RuntimeError("重试链全部 %d 次失败: %s" % (len(plan), attempts))
