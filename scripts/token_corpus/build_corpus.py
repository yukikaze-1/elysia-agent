"""token 估算基准语料的**唯一生成器**（Batch 18 / KB-12）。

    用法：conda run -n Elysia python scripts/token_corpus/build_corpus.py

输出到 `Module/system_prompt/tests/fixtures/token_corpus/`（签入产物）。
本文件放 `scripts/` 而不是 fixtures 目录里：它**不是测试代码**（不被收集、不在任何
测试档位里跑），而 fixtures 目录里的测试代码受"不得裸写 os.environ"等守卫管辖。

## 为什么语料要签入而不是每次随机生成

KB-12 的结论必须可复现：今天说"JSON 低估 40%"，下周必须还能跑出同一张表。
故本脚本**一次性**生成 `text/*.txt` 与 `chat/*.json` 并签入；测试只读签入产物。
本脚本保留在语料目录里是为了让"这些字符到底哪来的"可审计（§34），
**不参与**任何测试收集（文件名以 `_` 开头）。

## 内容纪律

- 全部为**本仓作者原创**的合成文本（无版权素材、无用户数据、无密钥）。
- 生成过程**确定**：固定句子池 + 轮转索引，无 RNG、无时间、无 locale 依赖。
- 语料分 `te`（调参/断言用）与 `holdout`（只在验证阶段读，**不参与任何调参**）
  两组；`category` 字段供按类别汇总误差。
"""

from __future__ import annotations

import json
from pathlib import Path

_HERE = (Path(__file__).resolve().parents[2]
         / "Module/system_prompt/tests/fixtures/token_corpus")

# --------------------------------------------------------------------------
# 句子池（原创合成；用于组装带变化的自然文本，避免"同一句重复 N 次"造成
# token 分布退化——重复文本在 BPE 下 tokenize 结果与自然文本差别很大）。
# --------------------------------------------------------------------------

_EN_SENTENCES = [
    "The morning light filtered through the curtains and painted pale stripes across the floor.",
    "She opened the notebook and began writing down everything she could still remember.",
    "There is a particular kind of silence that settles over a house after everyone has left.",
    "The engineer traced the failure back to a single unguarded assumption in the parser.",
    "Rain had been falling since dawn, and the streets shone like polished slate.",
    "He preferred to solve problems slowly, because speed had cost him dearly before.",
    "A well-designed interface disappears; the user simply does what they intended to do.",
    "The old library smelled of paper, dust, and the faint sweetness of decaying glue.",
    "They agreed to meet again when the work was finished, though neither believed it would be.",
    "Measurement without interpretation is just a pile of numbers waiting to mislead someone.",
    "The train pulled out of the station, and the platform emptied in a matter of seconds.",
    "Every abstraction leaks, and the ones we build ourselves leak in ways we cannot foresee.",
    "She kept a list of questions that she had never managed to answer satisfactorily.",
    "The garden had grown wild in the years since anyone had bothered to tend it.",
    "A simple rule applied consistently beats a complicated rule applied occasionally.",
    "He read the report twice and still could not decide whether it was honest or merely careful.",
    "The wind changed direction around midnight and brought the smell of the sea inland.",
    "Documentation is a promise about behaviour, and promises are easy to make and hard to keep.",
    "They walked along the ridge until the valley below turned the colour of old brass.",
    "The system behaved correctly for six months and then failed in a way nobody had predicted.",
    "Learning to delete code is harder than learning to write it, and far more valuable.",
    "The candle guttered, recovered, and settled into a small steady flame.",
    "He distrusted conclusions that arrived too quickly, especially his own.",
    "There is no such thing as a free invariant; every guarantee is paid for somewhere.",
    "The children ran ahead, shouting, while the adults followed at a more dignified pace.",
    "A test that never fails is either perfect or vacuous, and the second is far more common.",
    "She sorted the papers into three piles and then, thinking better of it, made four.",
    "The mountain path narrowed, and the drop on the left became uncomfortably persuasive.",
    "Good tools extend judgement; bad tools replace it with procedure.",
    "By evening the fog had lifted enough to reveal the outline of the far shore.",
]

_ZH_SENTENCES = [
    "清晨的光线穿过窗帘，在地板上留下一道道淡淡的条纹。",
    "她打开笔记本，开始把还能记起来的事情一件件写下来。",
    "所有人都离开之后，屋子里会沉下一种特别的安静。",
    "工程师把故障追溯到一个解析器里未经检查的假设。",
    "雨从黎明前就开始下，街道亮得像打磨过的石板。",
    "他更愿意慢慢地解决问题，因为图快曾经让他付出过代价。",
    "好的界面会消失，使用者只是做了他本来想做的事。",
    "旧图书馆里是纸张、灰尘和胶水缓慢变质的气味。",
    "他们说好等事情做完再见面，尽管两个人都不太相信。",
    "没有解释的测量只是一堆等着误导人的数字。",
    "火车驶出站台，月台在几秒钟之内就空了。",
    "每一个抽象都会泄漏，而自己造的抽象泄漏得最没有预兆。",
    "她保存了一份清单，上面全是她始终没能满意回答的问题。",
    "花园在无人照料的那几年里长得不成样子。",
    "一条被稳定执行的简单规则，胜过一条偶尔执行的复杂规则。",
    "他把报告读了两遍，仍然判断不出它是诚实还是仅仅谨慎。",
    "午夜前后风改了方向，把海的气息吹进了内陆。",
    "文档是关于行为的承诺，而承诺总是容易许下、难以遵守。",
    "他们沿着山脊一直走，直到下面的山谷变成旧黄铜的颜色。",
    "系统正确地运行了六个月，然后以一种没人预料到的方式失败了。",
    "学会删代码比学会写代码更难，也更有价值。",
    "蜡烛晃了一下，又稳住，变成一小簇平稳的火焰。",
    "他不信任来得太快的结论，尤其是自己得出的那些。",
    "没有免费的约束，每一条保证都在某个地方付了账。",
    "孩子们跑在前面喊叫，大人们用更体面的速度跟着。",
    "一个从不失败的测试要么完美，要么空洞，而后者常见得多。",
]

_ZH_TW_SENTENCES = [
    "清晨的光線穿過窗簾，在地板上留下一道道淡淡的條紋。",
    "她打開筆記本，開始把還能記起來的事情一件件寫下來。",
    "所有人都離開之後，屋子裡會沉下一種特別的安靜。",
    "工程師把故障追溯到一個解析器裡未經檢查的假設。",
    "雨從黎明前就開始下，街道亮得像打磨過的石板。",
    "他更願意慢慢地解決問題，因為圖快曾經讓他付出過代價。",
    "好的介面會消失，使用者只是做了他本來想做的事。",
    "舊圖書館裡是紙張、灰塵和膠水緩慢變質的氣味。",
    "他們說好等事情做完再見面，儘管兩個人都不太相信。",
    "沒有解釋的測量只是一堆等著誤導人的數字。",
]

_JA_SENTENCES = [
    "朝の光がカーテンを通り抜けて、床に淡い縞模様を描いていた。",
    "彼女はノートを開き、まだ覚えていることを書き留め始めた。",
    "みんなが出て行った後の家には、特別な静けさが残る。",
    "エンジニアは障害を、パーサーの中の検査されていない仮定までたどった。",
    "夜明け前から雨が降り続き、街路は磨かれた石版のように光っていた。",
    "彼は急ぐよりゆっくり解く方を好んだ。速さで痛い目を見たことがあったからだ。",
    "良いインターフェースは消える。利用者はただ意図したことをするだけだ。",
    "古い図書館は紙と埃と、ゆっくり劣化する糊の甘い匂いがした。",
    "計測に解釈がなければ、人を誤らせるのを待っている数字の山にすぎない。",
    "抽象はどれも漏れる。自分で作った抽象は、予想もしない所から漏れる。",
]

_FR_SENTENCES = [
    "La lumière du matin traversait les rideaux et dessinait de pâles rayures sur le sol.",
    "Elle ouvrit le carnet et commença à noter tout ce dont elle se souvenait encore.",
    "Il existe un silence particulier qui s'installe dans une maison après le départ de tous.",
    "L'ingénieur remonta la panne jusqu'à une hypothèse non vérifiée dans l'analyseur.",
    "La pluie tombait depuis l'aube et les rues brillaient comme de l'ardoise polie.",
]

_RU_SENTENCES = [
    "Утренний свет пробивался сквозь занавески и рисовал на полу бледные полосы.",
    "Она открыла блокнот и начала записывать всё, что ещё помнила.",
    "В доме после ухода всех остаётся особенная тишина.",
    "Инженер проследил сбой до непроверенного предположения в разборщике.",
]


def _cycle(pool: list[str], count: int, offset: int = 0) -> str:
    """从句子池按轮转索引取 count 句拼接（确定性；避免整段重复）。"""
    return " ".join(pool[(i + offset) % len(pool)] for i in range(count))


def _zh_cycle(pool: list[str], count: int, offset: int = 0) -> str:
    return "".join(pool[(i + offset) % len(pool)] for i in range(count))


# --------------------------------------------------------------------------
# 文本类语料
# --------------------------------------------------------------------------

def _text_cases() -> dict[str, tuple[str, str]]:
    """id → (category, text)。"""
    cases: dict[str, tuple[str, str]] = {}

    # --- English prose ---
    cases["english_short"] = ("english", "Hello there. How are you doing today?")
    cases["english_1k"] = ("english", _cycle(_EN_SENTENCES, 20))
    cases["english_10k"] = ("english", _cycle(_EN_SENTENCES, 200))
    cases["english_punct_heavy"] = (
        "english_punct",
        "Wait -- really?! (yes, really); #1 & #2, 3.14, 50%, A/B, x*y, [a-z]+; "
        "\"quoted\", 'single', ... <tag>, {json}, a|b, ~5, ^start, $end. " * 24,
    )
    cases["english_whitespace"] = (
        "whitespace",
        "def   spaced(  a ,   b ):\n\t\treturn   a\t+\tb\n\n\n"
        "value      =      42          # trailing    comment\n"
        + "\t" * 8 + "deep_indent = True\n" + " " * 40 + "wide_gap = 1\n",
    )

    # --- CJK ---
    cases["cjk_zh_simplified"] = ("cjk_zh", _zh_cycle(_ZH_SENTENCES, 20))
    cases["cjk_zh_traditional"] = ("cjk_zh_tw", _zh_cycle(_ZH_TW_SENTENCES, 10))
    cases["cjk_ja"] = ("cjk_ja", _zh_cycle(_JA_SENTENCES, 10))
    cases["cjk_punct_heavy"] = (
        "cjk_punct",
        "他说：「这样可以吗？」——「大概吧……」她停顿了一下，接着问："
        "『那么，下一步呢？』（括号里的内容其实并不重要！）《书名》·、；：？！…—～" * 8,
    )
    cases["cjk_no_space_long"] = (
        "cjk_no_space",
        "在没有任何空格的长句里分词器必须依靠上下文来判断词的边界这是对估算器最直接的考验"
        "因为字符数与词数的比值会明显高于平常的书面语于是按字符数换算的估算会系统性地偏高" * 6,
    )
    cases["cjk_mixed_zh_en"] = (
        "mixed_zh_en",
        _zh_cycle(_ZH_SENTENCES, 6)
        + " 请检查 API 返回的 JSON 字段 statusCode=200 与 requestId 0f8a-1c2d，"
        + "然后调用 `parse_response()` 解析它。" * 6,
    )

    # --- JSON ---
    records = [
        {"id": i, "name": f"item_{i:03d}", "score": round(i * 1.37, 2),
         "tags": ["alpha", "beta"], "active": i % 2 == 0}
        for i in range(40)
    ]
    cases["json_minified"] = ("json", json.dumps(records, separators=(",", ":"), ensure_ascii=False))
    cases["json_pretty"] = ("json", json.dumps(records[:20], indent=2, ensure_ascii=False))
    cases["json_nested"] = (
        "json",
        json.dumps(
            {"level1": {"level2": {"level3": {"level4": [
                {"key": f"k{i}", "meta": {"created": "2026-01-01T00:00:00Z", "rev": i}}
                for i in range(12)]}}}, "ok": True},
            indent=2, ensure_ascii=False),
    )
    cases["json_long_keys"] = (
        "json",
        json.dumps([
            {"user_preferred_display_name_override": f"name_{i}",
             "notification_delivery_channel_preference": "websocket",
             "maximum_retry_attempts_before_permanent_failure": i}
            for i in range(25)
        ], indent=2, ensure_ascii=False),
    )
    cases["json_numeric_arrays"] = (
        "json",
        json.dumps({"readings": [[round(i * 0.017, 4) for i in range(40)] for _ in range(12)],
                    "indices": list(range(200))}, separators=(",", ":"), ensure_ascii=False),
    )
    cases["json_escaped_strings"] = (
        "json",
        json.dumps({"path": "C:\\Users\\dev\\AppData\\Local\\Temp\\out.log",
                    "regex": "^[a-z]+\\d{2,4}\\s*$\n\t\"quoted\"",
                    "unicode": "中文\\u4e2d\\u6587 emoji \\ud83d\\ude00 tab\\t",
                    "multi": "line one\nline two\nline three"}, ensure_ascii=False),
    )
    cases["json_zh"] = (
        "json",
        json.dumps({"标题": "测试报告", "摘要": "这是一段中文摘要，用于检验结构化内容里的中日韩文字。",
                    "条目": [{"名称": f"项目{i}", "状态": "进行中", "备注": "无"} for i in range(15)]},
                   ensure_ascii=False, indent=2),
    )

    # --- Code ---
    cases["code_python"] = ("code", "\n\n".join(
        f"def transform_{i}(items: list[dict], *, limit: int = {i + 1}) -> dict[str, int]:\n"
        f"    \"\"\"Aggregate {i} values.\"\"\"\n"
        f"    total = 0\n"
        f"    for idx, item in enumerate(items):\n"
        f"        if item.get('active', False) and idx < limit:\n"
        f"            total += int(item['score']) * {i + 2}\n"
        f"    return {{'total': total, 'count': len(items)}}"
        for i in range(12)))
    cases["code_typescript"] = ("code", "\n\n".join(
        f"export interface Record{i} {{ id: string; value: number; tags: readonly string[] }}\n"
        f"export function select{i}(rows: readonly Record{i}[]): Record{i}[] {{\n"
        f"  return rows.filter((r) => r.value > {i} && r.tags.includes('keep'));\n"
        f"}}"
        for i in range(10)))
    cases["code_yaml"] = ("code", "\n".join(
        f"service_{i}:\n  image: registry.example.com/app:{i}.0\n  replicas: {i + 1}\n"
        f"  env:\n    - name: LOG_LEVEL\n      value: \"info\"\n    - name: RETRIES\n      value: \"{i}\"\n"
        f"  resources:\n    limits:\n      cpu: \"{i + 1}00m\"\n      memory: \"{i * 128}Mi\""
        for i in range(8)))
    cases["code_shell"] = ("code", "\n".join(
        f"for f in $(find ./data -name '*.json' -type f); do\n"
        f"  jq -r '.items[] | select(.score > {i}) | .name' \"$f\" >> out_{i}.txt\n"
        f"done\n" for i in range(8)))
    cases["code_sql"] = ("code", "\n".join(
        f"SELECT u.user_id, COUNT(*) AS n, AVG(o.total_amount) AS avg_total\n"
        f"FROM users u JOIN orders_{i} o ON o.user_id = u.user_id\n"
        f"WHERE o.created_at >= '2026-0{i % 9 + 1}-01' AND o.status <> 'cancelled'\n"
        f"GROUP BY u.user_id HAVING COUNT(*) > {i} ORDER BY avg_total DESC LIMIT 20;\n"
        for i in range(8)))
    cases["code_long_identifiers"] = (
        "code",
        "\n".join(
            f"const {name} = compute_{name}_with_fallback_and_retry_policy_v{i};"
            for i, name in enumerate([
                "very_long_snake_case_identifier_used_for_testing_boundaries",
                "another_extremely_descriptive_variable_name_here",
                "internal_configuration_override_for_the_secondary_endpoint",
                "maximum_number_of_retries_before_permanent_failure",
                "user_preferred_notification_delivery_channel_preference",
            ])),
    )

    # --- Markdown ---
    cases["markdown_mixed"] = (
        "markdown",
        "# 标题一\n\n## Section Two\n\nSome **bold** and *italic* text with `inline code`.\n\n"
        "- bullet one\n- bullet two\n  - nested bullet\n\n"
        "| Column A | Column B | Column C |\n|---|---|---|\n"
        + "\n".join(f"| row {i} | value {i * 2} | `code_{i}` |" for i in range(10))
        + "\n\n```python\nprint('fenced block')\n```\n\n"
        "[link text](https://example.com/path?q=1&r=2)\n\n> blockquote line\n",
    )

    # --- URLs / IDs / paths ---
    cases["urls_ids_paths"] = (
        "identifiers",
        " ".join([
            "https://api.example.com/v2/users/0f8a1c2d-3e4f-5a6b-7c8d-9e0f1a2b3c4d/orders?limit=50&offset=100",
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "/home/user/projects/elysia-agent/Module/system_prompt/budget.py",
            "camelCaseIdentifier", "snake_case_identifier", "SCREAMING_SNAKE_CASE",
            "9f8e7d6c-5b4a-3928-1706-0f1e2d3c4b5a", "v1.2.3-rc.4+build.5678",
        ] * 4),
    )

    # --- Unicode ---
    cases["unicode_emoji"] = ("unicode", "😀 🎉 🚀 ✨ 🔥 ❤️ 👍 🎯 🧠 🌙 " * 12)
    cases["unicode_zwj_combining"] = (
        "unicode",
        "👨‍👩‍👧‍👦 👩🏽‍💻 🏳️‍🌈 🇨🇳🇯🇵 éèêë ñ ç ü ö a\u0301e\u0301o\u0301 "
        "１２００ ＡＢＣ 。，、！？ " * 8,
    )
    cases["unicode_accents_cyrillic"] = (
        "unicode",
        " ".join(_FR_SENTENCES) + " " + " ".join(_RU_SENTENCES) + " "
        "Grüße aus München — Straße, ça va, año, Ελληνικά, עברית, العربية",
    )

    # --- 重复/退化输入（Challenger 面：自然文本之外的最坏一侧）---
    # 为什么必须有：BPE 的合并率完全取决于词表——`"你好" * N` 会合并（0.5 token/字），
    # 而 `"呀" * N` 不会（1.0 token/字）。自然语料只覆盖前者，若不显式纳入，
    # 估算器会在"重复单字"这一类上系统性低估（实测 -42%）。
    cases["cjk_repeat_single_char"] = ("repeat", "呀" * 300)
    cases["cjk_repeat_bigram"] = ("repeat", "你好" * 300)
    cases["cjk_punct_run"] = ("repeat", "。" * 300)
    cases["ascii_repeat_char"] = ("repeat", "a" * 400)
    cases["ascii_repeat_word"] = ("repeat", "word " * 100)

    # --- Small / empty ---
    cases["empty"] = ("small", "")
    cases["tiny_greeting"] = ("small", "hi")
    cases["tiny_zh_greeting"] = ("small", "你好")
    return cases


# --------------------------------------------------------------------------
# chat 形状语料（messages + tools）
# --------------------------------------------------------------------------

def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_TOOLS_SMALL = [
    _tool("get_time", "获取当前时间。", {"timezone": {"type": "string", "description": "IANA 时区名"}},
          ["timezone"]),
]

_TOOLS_MEDIUM = _TOOLS_SMALL + [
    _tool("search_memory", "在长期记忆中检索相关条目。",
          {"query": {"type": "string", "description": "检索查询"},
           "limit": {"type": "integer", "description": "返回条数上限", "default": 5},
           "memory_type": {"type": "string", "enum": ["fact", "preference", "note"]}},
          ["query"]),
    _tool("read_file", "读取工作目录下的文本文件。",
          {"path": {"type": "string", "description": "相对路径"},
           "max_bytes": {"type": "integer", "default": 65536}}, ["path"]),
    _tool("web_search", "执行一次网络搜索并返回摘要。",
          {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 5},
           "language": {"type": "string", "enum": ["zh", "en", "ja"]}}, ["query"]),
]

_TOOLS_MANY = _TOOLS_MEDIUM + [
    _tool(f"plugin_action_{i}",
          f"第 {i} 号插件动作：对目标对象执行声明式变更并返回执行结果摘要。",
          {"target": {"type": "string", "description": "目标标识"},
           "payload": {"type": "object", "description": "结构化载荷",
                       "properties": {"key": {"type": "string"}, "value": {"type": "string"}}},
           "dry_run": {"type": "boolean", "default": False},
           "timeout_seconds": {"type": "integer", "default": 30}},
          ["target", "payload"])
    for i in range(24)
]

_CHAT_CASES: dict[str, dict] = {
    "chat_single_turn": {
        "category": "chat_framing",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
    },
    "chat_system_user": {
        "category": "chat_framing",
        "messages": [
            {"role": "system", "content": "You are a concise assistant. Answer in one sentence."},
            {"role": "user", "content": "Summarize the purpose of a unit test."},
        ],
    },
    "chat_many_small_messages": {
        "category": "chat_framing",
        "messages": [{"role": "system", "content": "You are a helpful assistant."}]
        + [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(100)],
    },
    "chat_one_large_message": {
        "category": "chat_framing",
        "messages": [{"role": "system", "content": "You are a helpful assistant."},
                     {"role": "user", "content": _cycle(_EN_SENTENCES, 100)}],
    },
    "chat_role_mixture": {
        "category": "chat_framing",
        "messages": [
            {"role": "system", "content": "你是 Elysia，一个桌面 AI 伙伴。回答要简短。"},
            {"role": "user", "content": "帮我看看这段配置有什么问题。"},
            {"role": "assistant", "content": "好的，请把配置贴出来。"},
            {"role": "user", "content": "retries: -1\nworkers: 0\ntimeout: 0"},
            {"role": "assistant", "content": "retries 不能为负，workers 至少为 1，timeout 不能为 0。"},
            {"role": "user", "content": "那应该怎么改？"},
        ],
    },
    "chat_tool_history": {
        "category": "chat_framing",
        "messages": [
            {"role": "system", "content": "你是 Elysia。"},
            {"role": "user", "content": "北京今天天气怎么样？"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "web_search", "arguments": "{\"query\": \"北京天气\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴，24°C，微风。"},
            {"role": "assistant", "content": "北京今天晴，24 度，有微风。"},
            {"role": "user", "content": "那明天呢？"},
        ],
    },
    "chat_empty_messages": {
        "category": "chat_framing",
        "messages": [{"role": "system", "content": ""}, {"role": "user", "content": ""}],
    },
    "tools_small": {
        "category": "tools",
        "messages": [{"role": "user", "content": "现在几点？"}],
        "tools": _TOOLS_SMALL,
    },
    "tools_medium": {
        "category": "tools",
        "messages": [{"role": "user", "content": "现在几点？"}],
        "tools": _TOOLS_MEDIUM,
    },
    "tools_many": {
        "category": "tools",
        "messages": [{"role": "user", "content": "现在几点？"}],
        "tools": _TOOLS_MANY,
    },
    "tools_many_short_chat": {
        "category": "tools",
        "messages": [{"role": "user", "content": "在吗"}],
        "tools": _TOOLS_MANY,
    },
    "representative_elysia_prompt": {
        "category": "representative",
        "messages": [
            {"role": "system", "content":
                "你是 Elysia，一个长期陪伴用户的桌面 AI 伙伴。\n\n"
                "## 冲突规则\n当指令冲突时，安全边界优先于一切，其次是输出格式，再次是人格表达。\n\n"
                "## 输出格式\n每次回复必须包含一个 <inner_voice> 区块，用一到两句话说明当前感受与本次回应意图。\n\n"
                + _zh_cycle(_ZH_SENTENCES, 8) + "\n\n"
                "## 长期记忆\n" + _zh_cycle(_ZH_SENTENCES, 6, offset=3) + "\n\n"
                "## 环境\n当前时间：2026-09-19 21:04，用户正在使用桌面客户端。"},
            {"role": "user", "content": "我们上次聊到哪里了？"},
            {"role": "assistant", "content": "上次我们在聊你那个解析器的故障定位。"},
            {"role": "user", "content": "对，后来我改了那段代码，你帮我看看现在的写法有没有问题："},
            {"role": "user", "content": "def parse(payload: str) -> dict:\n"
                                       "    return json.loads(payload) if payload else {}"},
            {"role": "user", "content": "另外这是接口返回：" +
             json.dumps({"status": "ok", "items": [{"id": i, "name": f"n{i}"} for i in range(20)]},
                        ensure_ascii=False)},
        ],
        "tools": _TOOLS_MEDIUM,
    },
}

# --------------------------------------------------------------------------
# holdout：只在验证阶段读，**不参与任何调参**（§165/§166）
# --------------------------------------------------------------------------

_HOLDOUT_TEXT = {
    "holdout_english": (
        "english",
        "The lighthouse keeper recorded the weather every four hours, noting the direction of "
        "the swell and whether the gulls had moved inland. On the ninth day a fishing boat "
        "appeared from the north, running without lights, and passed so close that he could "
        "hear the engine labouring against the tide. He wrote that down too."),
    "holdout_cjk": (
        "cjk_zh",
        "气象站的值班员每四小时记录一次天气，写下涌浪的方向，以及海鸥有没有往内陆飞。"
        "第九天，一条渔船从北面出现，没有点灯，靠得极近，近到他能听见发动机逆着潮水吃力地运转。"
        "他把这件事也记了下来。"),
    "holdout_json": (
        "json",
        json.dumps({"schemaVersion": "2.4.1",
                    "pipelines": [{"name": f"pipe-{i}", "stages": ["fetch", "parse", "store"],
                                   "retry": {"max": i + 1, "backoffMs": 250 * (i + 1)},
                                   "enabled": bool(i % 3)}
                                  for i in range(12)]},
                   ensure_ascii=False, separators=(",", ":"))),
    "holdout_code": (
        "code",
        "\n\n".join(
            f"func Handle{i}(w http.ResponseWriter, r *http.Request) {{\n"
            f"\tctx, cancel := context.WithTimeout(r.Context(), {i + 1}*time.Second)\n"
            f"\tdefer cancel()\n\tif err := svc.Process(ctx, r.URL.Query().Get(\"id\")); err != nil {{\n"
            f"\t\thttp.Error(w, err.Error(), http.StatusBadGateway)\n\t\treturn\n\t}}\n"
            f"\tw.WriteHeader(http.StatusNoContent)\n}}"
            for i in range(8))),
    "holdout_mixed": (
        "mixed_zh_en",
        "把 timeout 从 30 秒改成 5 秒之后，worker 会频繁超时，日志里出现 "
        "`context deadline exceeded` 与 retryCount=3。建议改回 15 秒，或者把 batchSize 调小。" * 5),
    "holdout_markdown": (
        "markdown",
        "## 变更记录\n\n- **修复**：`estimate_tokens` 对结构化内容的低估\n"
        "- **新增**：`token_corpus` 基准语料\n\n"
        "| 字段 | 类型 | 说明 |\n|---|---|---|\n"
        + "\n".join(f"| field_{i} | `str` | 第 {i} 个字段的说明文字 |" for i in range(8))
        + "\n\n```bash\nmake test-quick\n```\n"),
}


def main() -> None:
    text_dir = _HERE / "text"
    chat_dir = _HERE / "chat"
    text_dir.mkdir(exist_ok=True)
    chat_dir.mkdir(exist_ok=True)

    manifest: list[dict] = []
    for case_id, (category, text) in sorted(_text_cases().items()):
        path = text_dir / f"{case_id}.txt"
        path.write_text(text, encoding="utf-8")
        manifest.append({"id": case_id, "kind": "text", "category": category,
                         "path": f"text/{case_id}.txt", "chars": len(text),
                         "bytes": len(text.encode("utf-8")), "set": "te"})

    for case_id, payload in sorted(_CHAT_CASES.items()):
        path = chat_dir / f"{case_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        manifest.append({"id": case_id, "kind": "chat", "category": payload["category"],
                         "path": f"chat/{case_id}.json", "set": "te"})

    for case_id, (category, text) in sorted(_HOLDOUT_TEXT.items()):
        path = text_dir / f"{case_id}.txt"
        path.write_text(text, encoding="utf-8")
        manifest.append({"id": case_id, "kind": "text", "category": category,
                         "path": f"text/{case_id}.txt", "chars": len(text),
                         "bytes": len(text.encode("utf-8")), "set": "holdout"})

    (_HERE / "MANIFEST.json").write_text(
        json.dumps({"entries": manifest}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(manifest)} entries "
          f"({sum(1 for m in manifest if m['set'] == 'te')} te / "
          f"{sum(1 for m in manifest if m['set'] == 'holdout')} holdout)")


if __name__ == "__main__":
    main()
