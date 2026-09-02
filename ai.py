"""AI 分析层 —— 任意 OpenAI 兼容接口（OpenAI / DeepSeek / GLM / Kimi / Ollama 等）。

环境变量：
  OPENAI_BASE_URL  接口地址，默认 https://api.openai.com/v1
  OPENAI_API_KEY   密钥
  OPENAI_MODEL     模型名，如 gpt-4o / deepseek-chat / glm-4-plus / qwen-max
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx


def _cfg() -> tuple[str, str, str]:
    return (
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        os.environ.get("OPENAI_API_KEY", ""),
        os.environ.get("OPENAI_MODEL", "gpt-4o"),
    )


def ai_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def call_text(prompt: str, max_tokens: int = 8192, system: Optional[str] = None) -> str:
    base, key, model = _cfg()
    if not key:
        raise RuntimeError("AI 未配置：请设置 OPENAI_API_KEY")

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "messages": messages},
        timeout=300,
    )
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"AI 调用失败: {json.dumps(data['error'], ensure_ascii=False)[:500]}")
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"AI 返回结构异常: {json.dumps(data, ensure_ascii=False)[:500]}")


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


SYSTEM = """你是一位资深面试教练，同时具备一线大厂面试官（技术/产品/管理）和职业辅导的双重经验。
你的任务是基于面试录音转写文本，为候选人做严格、具体、可执行的复盘。

要求：
- 严格：不要给安慰式的高分。评分参照真实大厂标准，普通回答就是 5-6 分。
- 具体：指出问题必须引用候选人原话的关键片段，不要笼统说"表达不够清晰"。
- 可执行：改进建议要能直接照着改，示范答案要能直接背下来用。
- 诚实：转写质量差、信息不足的地方直接说明，不要编造面试内容。"""


PROMPT_TMPL = """下面是一场面试的录音转写文本。说话人可能未区分，请你根据语义自行判断哪些是面试官提问、哪些是候选人（我）的回答。

## 面试背景
- 公司：{company}
- 岗位：{role_title}
- 轮次：{round_name}

## 转写文本
<transcript>
{transcript}
</transcript>
{history_block}
请输出一个 JSON 对象（不要输出任何其他文字），结构如下：

{{
  "overall_score": 整数 0-100，本场综合表现分,
  "verdict": "一句话总评，不超过 60 字，直接说这场面试的核心问题或亮点",
  "pass_likelihood": "高" | "中" | "低",
  "summary": "3-5 句话的整体复盘：面试整体节奏、我的表现基调、最该改的一件事",
  "qa_items": [
    {{
      "seq": 1,
      "category": "问题类型，从以下选一个：自我介绍/项目深挖/专业能力/业务理解/方法论/协作沟通/压力提问/职业规划/反问环节/其他",
      "question": "面试官提出的问题（按语义整理，不要照抄口语碎片）",
      "answer_digest": "我的回答要点概括，2-3 句",
      "score": 整数 0-10,
      "strengths": "这题答得好的地方；确实没有就写「无明显亮点」",
      "issues": "这题的具体问题，必须引用我的原话片段作为证据",
      "better_answer": "更优答案示范。用我原有的真实素材重组，采用 STAR 或结论先行结构，写成可以直接说出口的第一人称口语，150-300 字。这题已经答得很好则写「本题回答已达标，无需重写」"
    }}
  ],
  "speech_habits": {{
    "filler_words": [{{"word": "口头禅原词", "count": 出现次数估计}}],
    "pace": "语速与节奏评价，结合转写里的重复、卡顿、自我打断现象",
    "verbosity": "啰嗦度评价：有没有答非所问、绕圈、一个问题讲太久",
    "structure": "表达结构评价：是否结论先行、有没有逻辑连接词",
    "top_fixes": ["最该立刻改掉的表达习惯，3 条，每条一句话且可执行"]
  }},
  "interviewer_focus": {{
    "signals": ["从面试官的提问方式、追问点、打断时机推断出他真正关心什么，3-5 条"],
    "unmet_expectations": ["面试官明显想听到但我没给出的东西，2-4 条"],
    "next_round_prep": ["据此推断下一轮该重点准备什么，3 条"]
  }},
  "weakest_topics": ["本场最薄弱的 2-3 个能力主题，用简短名词短语"],
  "action_items": ["下场面试前的具体行动清单，3-5 条，每条都能在一周内完成"]{progress_field}
}}

注意：
- qa_items 覆盖转写里所有实质性问答，通常 5-15 条；不要遗漏反问环节。
- 转写里如果有大段内容无法判断归属或质量太差，在 summary 里说明，不要硬编内容。
- 全部输出中文。"""


PROGRESS_FIELD = """,
  "progress": {{
    "compared_rounds": "说明本次与哪几场历史面试做了对比",
    "improved": ["相比历史面试确实进步的点，引用历史与本次的具体差异，2-4 条"],
    "regressed": ["退步或反复出现的点，2-4 条"],
    "recurring_issues": [
      {{"issue": "跨场次反复出现的问题", "times": 出现过的场次数, "verdict": "这个问题这次有没有改善"}}
    ],
    "trend": "整体趋势判断：在变好 / 原地踏步 / 在变差，并给出理由"
  }}"""


def _history_block(history: list[dict]) -> str:
    if not history:
        return "\n"
    lines = ["\n## 我的历史面试记录（用于跨场次进步追踪）"]
    for h in history:
        lines.append(
            f"\n### {h.get('interview_date') or '未填日期'} | {h.get('company') or '未填公司'}"
            f" | {h.get('role_title') or '未填岗位'} | {h.get('round_name') or '未填轮次'}"
            f" | 综合分 {h.get('overall_score')}"
        )
        a = h.get("analysis") or {}
        if a.get("verdict"):
            lines.append(f"- 总评：{a['verdict']}")
        if a.get("weakest_topics"):
            lines.append(f"- 当时的薄弱项：{'、'.join(a['weakest_topics'][:5])}")
        habits = a.get("speech_habits") or {}
        if habits.get("top_fixes"):
            lines.append(f"- 当时提出的表达改进项：{'；'.join(habits['top_fixes'][:3])}")
        if a.get("action_items"):
            lines.append(f"- 当时的行动项：{'；'.join(a['action_items'][:4])}")
        weak_qa = [q for q in (h.get("qa_items") or []) if (q.get("score") or 10) <= 6]
        if weak_qa:
            brief = "；".join(
                f"[{q.get('category') or '其他'}] {(q.get('question') or '')[:40]}(得分{q.get('score')})"
                for q in weak_qa[:6]
            )
            lines.append(f"- 当时答得差的题：{brief}")
    lines.append("\n请在 progress 字段里做真实对比，不要泛泛而谈。\n")
    return "\n".join(lines)


MAX_TRANSCRIPT = 60000


def analyze_interview(
    transcript: str,
    company: str = "",
    role_title: str = "",
    round_name: str = "",
    history: Optional[list[dict]] = None,
) -> dict:
    history = history or []
    text = transcript.strip()
    if len(text) > MAX_TRANSCRIPT:
        text = text[:MAX_TRANSCRIPT] + "\n…（转写过长已截断）"

    prompt = PROMPT_TMPL.format(
        company=company or "未填写",
        role_title=role_title or "未填写",
        round_name=round_name or "未填写",
        transcript=text,
        history_block=_history_block(history),
        progress_field=PROGRESS_FIELD if history else "",
    )
    raw = call_text(prompt, max_tokens=16000, system=SYSTEM)
    return _extract_json(raw)


def polish_transcript(raw_text: str) -> str:
    """把 ASR 转写整理成分角色的对话稿，保留口头禅用于诊断。"""
    text = raw_text.strip()
    if len(text) > MAX_TRANSCRIPT:
        text = text[:MAX_TRANSCRIPT]
    prompt = f"""下面是一段面试录音的语音识别转写。请整理成可读的对话稿：

1. 按语义判断说话人，用「面试官：」和「我：」标注（识别不出说话人的段落原样保留）
2. 保持标点合理，明显的同音识别错误按上下文修正（专业术语纠正回来）
3. 保留原始表达习惯，不要润色回答内容——口头禅、重复、卡顿要保留，后续要用来诊断表达习惯
4. 只输出整理后的对话稿，不要任何说明文字

原文：
{text}"""
    return call_text(prompt, max_tokens=16000).strip()
