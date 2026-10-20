"""路径与运行参数。所有凭据都从文件读，绝不进环境变量或命令行。"""
import json
import os

BASE = os.environ.get("LPW_BASE", "/opt/leadpulse/worker")
SECRETS = os.environ.get("LPW_SECRETS", "/etc/leadpulse")
GRAPH_SECRETS = os.environ.get("LPW_GRAPH_SECRETS", "/etc/leadpulse/graph")
STATE_DIR = os.environ.get("LPW_STATE", "/var/lib/leadpulse")
KB_DIR = os.path.join(BASE, "kb")

SPOOL_FILE = os.environ.get(
    "AIW_SPOOL", "/var/lib/leadpulse/spool/events.jsonl")
OFFSET_FILE = os.path.join(STATE_DIR, "spool-offset.json")
PENDING_FILE = os.path.join(STATE_DIR, "pending.json")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = os.environ.get("AIW_MODEL", "deepseek-flash")

# --- 模型重试链：免费池优先，官方兜底 ---
# 为什么重试 auto 有意义：auto 每次路由到【不同】后端模型，
# 所以第二次尝试不是「重复同一个错误」，而是「换一个模型再试」。
# 实测 auto 单次成功率约 86.7%（空响应/JSON 截断），重试两次约 98%。
FREELLM_URL = os.environ.get(
    "AIW_FREELLM_URL", "http://127.0.0.1:3001/v1/chat/completions")
FREE_LLM_MODEL = os.environ.get("AIW_FREE_MODEL", "auto:shitu")
FREE_ATTEMPTS = int(os.environ.get("AIW_FREE_ATTEMPTS", "1"))
FREE_TIMEOUT = int(os.environ.get("AIW_FREE_TIMEOUT", "30"))
FALLBACK_MODEL = os.environ.get("AIW_MODEL", "deepseek-flash")
TEMPERATURE = 0.3
MAX_TOKENS = int(os.environ.get("AIW_MAX_TOKENS", "2000"))  # 900 太小，知识库变丰富后回复会超长被截断
LLM_TIMEOUT = 60

# 发布开关。默认关闭：骨架阶段只生成建议，不会真的发到 Facebook。
POST_ENABLED = os.environ.get("AIW_POST_ENABLED", "false").strip().lower() == "true"

POLL_SECONDS = 2.0
# spool 循环连续失败多少次后主动退出（让 systemd 重启），避免「活着但不工作」
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("AIW_MAX_FAILURES", "15"))

# 线索「取件箱」记录的保留天数（供卡片「复制建议回复」按钮回查原文）。
# 注意：评论待办记录不适用 —— 那些等人工点「发送 / 忽略」才清除。
PENDING_LEAD_TTL_DAYS = int(os.environ.get("AIW_LEAD_TTL_DAYS", "15"))
DEDUP_TTL = 6 * 3600

# 主页语言：声明语言的主页走该语言的提示词；
# 未声明时的回退。en 是开源默认值；
# 设 AIW_DEFAULT_PAGE_LANGUAGE=zh 可让中文主页走中文提示词。
DEFAULT_PAGE_LANGUAGE = os.environ.get("AIW_DEFAULT_PAGE_LANGUAGE", "en").strip().lower() or "en"

def read_secret(name, base=SECRETS):
    with open(os.path.join(base, name), "r", encoding="utf-8") as handle:
        return handle.read().strip()


def page_token_file(page_id):
    return os.path.join(GRAPH_SECRETS, "page-token-%s" % page_id)


# ---------- Meta 询盘线索（Instant Form leadgen）----------

# 多维表格：一条线索一行。表结构由部署脚本一次性建立，运行时不再改动。
BITABLE_HOST = os.environ.get("AIW_BITABLE_HOST", "")
BITABLE_APP_TOKEN = os.environ.get("AIW_BITABLE_APP", "")
BITABLE_TABLE_ID = os.environ.get("AIW_BITABLE_TABLE", "")
BITABLE_VIEW_ID = os.environ.get("AIW_BITABLE_VIEW", "")

# 去重与状态：独立 SQLite 单文件（无端口、无新进程）
LEAD_DB = os.path.join(STATE_DIR, "leads.db")

# 线索卡片的默认落点。留空则回退到 chat-routing.json 的第一个群。
LEAD_CHAT_ID = os.environ.get("AIW_LEAD_CHAT", "")

# 线索要输出五段分析，比评论回复长，900 会被截断
LEAD_MAX_TOKENS = int(os.environ.get("AIW_LEAD_MAX_TOKENS", "1500"))
LEAD_FALLBACK_MODEL = os.environ.get("AIW_LEAD_FALLBACK_MODEL", FALLBACK_MODEL)

# ---------- 飞书私信自由对话 ----------
# 与 Meta 评论回复分开配置：那边要结构化 JSON + 知识库约束、温度 0.3；
# 这边是自由聊天，超时更短、温度更高。
CHAT_MODEL = os.environ.get("AIW_CHAT_MODEL", "auto")
CHAT_ATTEMPTS = int(os.environ.get("AIW_CHAT_ATTEMPTS", "2"))
CHAT_TIMEOUT = int(os.environ.get("AIW_CHAT_TIMEOUT", "12"))
CHAT_MAX_TOKENS = int(os.environ.get("AIW_CHAT_MAX_TOKENS", "2500"))
CHAT_TEMPERATURE = float(os.environ.get("AIW_CHAT_TEMPERATURE", "0.7"))
CHAT_HISTORY_FILE = os.path.join(STATE_DIR, "dm-history.json")
# 本窗口私有的长期记忆（与 Kai 的 kai-mem 物理隔离）
CHAT_MEMORY_FILE = os.path.join(STATE_DIR, "dm-memory.json")

# ---------- 业务分类（写进提示词的 domain 字段）----------
# 默认只有一个兜底分类。按你的业务配置，例如：
#   AIW_DOMAIN_ENUM="hardware,software,other"
#   AIW_DOMAIN_GUIDE="  · hardware：硬件采购与规格\n  · software：软件授权与集成"
DOMAIN_ENUM = os.environ.get("AIW_DOMAIN_ENUM", "general,other")
DOMAIN_GUIDE = os.environ.get("AIW_DOMAIN_GUIDE", "")

# ---------- 询盘（Instant Form）----------
# 一句话说明公司做什么，会写进询盘分析提示词的开头。
LEAD_CONTEXT = os.environ.get(
    "AIW_LEAD_CONTEXT", "这是一家 B2B 制造商，通过 Facebook 表单收集客户询盘。")

# 表单选项 -> 内部名称的映射。留空则原样透传。见 lead-form-fields.example.json
FORM_FIELDS_FILE = os.environ.get(
    "AIW_FORM_FIELDS", os.path.join(BASE, "lead-form-fields.json"))


def pages():
    """返回 [(page_id, 显示名, 知识库 slug)]。

    优先读 pages.json；文件不存在时退化为扫描令牌目录 —— 这样最少只需要
    放好令牌就能跑，不必再维护一份重复的主页清单。
    """
    config_file = os.environ.get("PAGES_CONFIG_FILE",
                                 os.path.join(SECRETS, "pages.json"))
    try:
        with open(config_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data:
            return [(str(pid), (val or {}).get("name") or str(pid),
                     (val or {}).get("kb") or "",
                     (val or {}).get("lang") or DEFAULT_PAGE_LANGUAGE)
                    for pid, val in data.items()]
    except FileNotFoundError:
        pass
    except Exception:
        pass

    try:
        names = sorted(os.listdir(GRAPH_SECRETS))
    except OSError:
        return []
    return [(n[len("page-token-"):], n[len("page-token-"):], "", DEFAULT_PAGE_LANGUAGE)
            for n in names if n.startswith("page-token-")]


def page_language(page_id):
    """返回指定主页的语言代码（zh / en）。未声明时回退到 DEFAULT_PAGE_LANGUAGE。"""
    config_file = os.environ.get("PAGES_CONFIG_FILE",
                                 os.path.join(SECRETS, "pages.json"))
    try:
        with open(config_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data:
            val = data.get(str(page_id))
            if isinstance(val, dict):
                lang = (val.get("lang") or "").strip().lower()
                if lang:
                    return lang
    except (FileNotFoundError, OSError):
        pass
    except Exception:
        pass
    return DEFAULT_PAGE_LANGUAGE
