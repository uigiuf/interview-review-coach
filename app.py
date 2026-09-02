"""面试复盘教练 —— 个人版（服务端 ASR + 异步任务架构）。

流程：上传录音 / 粘贴文字 → 后台串行「转写 → 整理成对话稿 → AI 分析 → 报告」→
前端轮询状态实时看进度。

本地运行：
    pip install -r requirements.txt
    cp .env.example .env  # 填好各项后 source 或用 python-dotenv 加载
    uvicorn app:app --port 8000
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

import ai as ai_mod
import asr as asr_mod
import db as db_mod

_BASE = Path(__file__).resolve().parent
_schema_ready = False
_schema_lock = threading.Lock()

app = FastAPI(title="面试复盘教练")


# --------------------------- 用户识别 ---------------------------

def _require_user(x_user_email: Optional[str]) -> dict:
    email = (x_user_email or "").strip() or os.environ.get("AUTH_EMAIL", "").strip() or "me@localhost"
    return {"email": email, "name": "我"}


def _need_db() -> None:
    if not db_mod.db_ready():
        raise HTTPException(503, "数据库未配置：请设置 DATABASE_URL 或 PGHOST 等环境变量")
    try:
        _ensure_schema()
    except Exception as e:
        raise HTTPException(503, f"数据库初始化失败：{e}")


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        db_mod.init_schema()
        _schema_ready = True


# --------------------------- 基础路由 ---------------------------

@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_BASE / "static" / "index.html", media_type="text/html")


@app.get("/app.js")
def appjs() -> FileResponse:
    return FileResponse(_BASE / "static" / "app.js", media_type="application/javascript")


@app.get("/app.css")
def appcss() -> FileResponse:
    return FileResponse(_BASE / "static" / "app.css", media_type="text/css")


@app.get("/api/me")
def me(x_user_email: Optional[str] = Header(None, alias="X-User-Email")) -> JSONResponse:
    user = _require_user(x_user_email)
    if db_mod.db_ready():
        try:
            _ensure_schema()
            db_mod.upsert_user(user)
        except Exception:
            pass
    return JSONResponse(
        {
            "email": user["email"],
            "name": user["name"],
            "aiReady": ai_mod.ai_available(),
            "asrReady": asr_mod.asr_available(),
            "dbReady": db_mod.db_ready(),
            "maxAudioMB": MAX_AUDIO_MB,
        }
    )


# --------------------------- 面试记录 CRUD ---------------------------

def _row_to_brief(r: dict) -> dict:
    a = r.get("analysis") or {}
    return {
        "id": r["id"],
        "company": r.get("company"),
        "roleTitle": r.get("role_title"),
        "roundName": r.get("round_name"),
        "interviewDate": str(r["interview_date"]) if r.get("interview_date") else None,
        "status": r.get("status"),
        "stage": r.get("stage"),
        "overallScore": r.get("overall_score"),
        "outcome": r.get("outcome"),
        "verdict": a.get("verdict"),
        "passLikelihood": a.get("pass_likelihood"),
        "weakestTopics": a.get("weakest_topics") or [],
        "hasAudio": bool(r.get("audio_oid")),
        "audioName": r.get("audio_name"),
        "durationSec": r.get("duration_sec"),
        "errorMsg": r.get("error_msg"),
        "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
    }


@app.get("/api/interviews")
def list_interviews(
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> JSONResponse:
    user = _require_user(x_user_email)
    _need_db()
    rows = db_mod.query(
        """
        SELECT id, company, role_title, round_name, interview_date, status, stage,
               overall_score, outcome, analysis, audio_oid, audio_name,
               duration_sec, error_msg, created_at
        FROM interviews WHERE user_email = %s
        ORDER BY COALESCE(interview_date, created_at::date) DESC, id DESC
        """,
        (user["email"],),
    )
    return JSONResponse({"items": [_row_to_brief(r) for r in rows]})


@app.get("/api/interviews/{iid}")
def get_interview(
    iid: int,
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> JSONResponse:
    user = _require_user(x_user_email)
    _need_db()
    row = db_mod.query_one(
        "SELECT * FROM interviews WHERE id = %s AND user_email = %s", (iid, user["email"])
    )
    if not row:
        raise HTTPException(404, "记录不存在")
    qa = db_mod.query(
        """
        SELECT seq, category, question, answer_digest, score,
               strengths, issues, better_answer
        FROM qa_items WHERE interview_id = %s ORDER BY seq
        """,
        (iid,),
    )
    detail = _row_to_brief(row)
    detail["transcript"] = row.get("transcript")
    detail["rawTranscript"] = row.get("raw_transcript")
    detail["analysis"] = row.get("analysis") or {}
    detail["qaItems"] = qa
    detail["source"] = row.get("source")
    return JSONResponse(detail)


@app.get("/api/interviews/{iid}/audio")
def get_audio(
    iid: int,
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> Response:
    user = _require_user(x_user_email)
    _need_db()
    row = db_mod.query_one(
        "SELECT audio_oid, audio_type FROM interviews WHERE id = %s AND user_email = %s",
        (iid, user["email"]),
    )
    if not row or not row.get("audio_oid"):
        raise HTTPException(404, "没有录音文件")
    blob = db_mod.read_audio(row["audio_oid"])
    return Response(
        content=blob,
        media_type=row.get("audio_type") or "audio/mpeg",
        headers={"Accept-Ranges": "none", "Cache-Control": "private, max-age=3600"},
    )


@app.delete("/api/interviews/{iid}")
def delete_interview(
    iid: int,
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> JSONResponse:
    user = _require_user(x_user_email)
    _need_db()
    row = db_mod.query_one(
        "SELECT audio_oid FROM interviews WHERE id = %s AND user_email = %s", (iid, user["email"])
    )
    if not row:
        raise HTTPException(404, "记录不存在")
    if row.get("audio_oid"):
        db_mod.drop_audio(row["audio_oid"])
    db_mod.execute("DELETE FROM interviews WHERE id = %s AND user_email = %s", (iid, user["email"]))
    return JSONResponse({"ok": True})


@app.post("/api/interviews/{iid}/outcome")
async def set_outcome(
    iid: int,
    outcome: str = Form(...),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> JSONResponse:
    user = _require_user(x_user_email)
    _need_db()
    if outcome not in ("pending", "passed", "rejected", "offer"):
        raise HTTPException(400, "非法结果值")
    db_mod.execute(
        "UPDATE interviews SET outcome = %s, updated_at = NOW() WHERE id = %s AND user_email = %s",
        (outcome, iid, user["email"]),
    )
    return JSONResponse({"ok": True})


# --------------------------- 上传 + 异步流水线 ---------------------------

MAX_AUDIO_MB = int(os.environ.get("MAX_AUDIO_MB", "25"))  # Whisper 官方 25MB；自托管可调高


@app.post("/api/interviews")
async def create_interview(
    company: str = Form(""),
    role_title: str = Form(""),
    round_name: str = Form(""),
    interview_date: str = Form(""),
    transcript: str = Form(""),
    source: str = Form("upload"),
    duration_sec: str = Form(""),
    audio: Optional[UploadFile] = File(None),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> JSONResponse:
    user = _require_user(x_user_email)
    _need_db()

    text = (transcript or "").strip()
    audio_bytes = b""
    if audio is not None and audio.filename:
        audio_bytes = await audio.read()
        if len(audio_bytes) > MAX_AUDIO_MB * 1024 * 1024:
            raise HTTPException(400, f"录音文件超过 {MAX_AUDIO_MB}MB，请压缩或分段后再传")
        if audio_bytes and not asr_mod.asr_available():
            raise HTTPException(503, "服务端未配置 ASR：请设置 ASR_API_KEY 后再上传录音，或改用粘贴文字模式")

    if not audio_bytes and len(text) < 50:
        raise HTTPException(400, "内容太短：请上传录音，或至少粘贴 50 字以上的转写")

    audio_oid = None
    audio_name = audio_size = audio_type = None
    if audio_bytes:
        audio_oid = db_mod.save_audio(audio_bytes)
        audio_name = audio.filename
        audio_size = len(audio_bytes)
        audio_type = audio.content_type or "audio/mpeg"

    dur = None
    try:
        dur = int(float(duration_sec)) if duration_sec else None
    except Exception:
        dur = None

    initial_status = "queued" if audio_bytes else "analyzing"
    initial_stage = "等待转写" if audio_bytes else "分析中"

    row = db_mod.query_one(
        """
        INSERT INTO interviews
          (user_email, company, role_title, round_name, interview_date, source,
           transcript, raw_transcript, audio_oid, audio_name, audio_size, audio_type,
           duration_sec, status, stage, outcome)
        VALUES (%s, %s, %s, %s, NULLIF(%s,'')::date, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (
            user["email"], company.strip(), role_title.strip(), round_name.strip(),
            interview_date.strip(), source,
            text if not audio_bytes else "",   # 纯粘贴时先落 transcript；有音频等转写完再回填
            text if not audio_bytes else "",   # raw_transcript：粘贴稿也存一份
            audio_oid, audio_name, audio_size, audio_type, dur,
            initial_status, initial_stage,
        ),
    )
    iid = row["id"]

    threading.Thread(target=_run_pipeline, args=(iid, user["email"], bool(audio_bytes)), daemon=True).start()
    return JSONResponse({"id": iid, "status": initial_status})


@app.post("/api/interviews/{iid}/reanalyze")
def reanalyze(
    iid: int,
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> JSONResponse:
    user = _require_user(x_user_email)
    _need_db()
    row = db_mod.query_one(
        "SELECT id, audio_oid, transcript FROM interviews WHERE id = %s AND user_email = %s",
        (iid, user["email"]),
    )
    if not row:
        raise HTTPException(404, "记录不存在")
    has_transcript = bool((row.get("transcript") or "").strip())
    if not has_transcript and not row.get("audio_oid"):
        raise HTTPException(400, "该记录既无转写也无录音，无法重新分析")

    db_mod.execute(
        "UPDATE interviews SET status='analyzing', stage='分析中', error_msg=NULL, updated_at=NOW() WHERE id=%s",
        (iid,),
    )
    # 已有转写就直接进 analyze；没有就重跑整条流水线
    need_asr = not has_transcript and bool(row.get("audio_oid"))
    threading.Thread(target=_run_pipeline, args=(iid, user["email"], need_asr), daemon=True).start()
    return JSONResponse({"id": iid, "status": "analyzing"})


def _load_history(email: str, exclude_id: int, limit: int = 4) -> list[dict]:
    rows = db_mod.query(
        """
        SELECT id, company, role_title, round_name, interview_date, overall_score, analysis
        FROM interviews
        WHERE user_email = %s AND id <> %s AND status = 'done'
        ORDER BY COALESCE(interview_date, created_at::date) DESC, id DESC
        LIMIT %s
        """,
        (email, exclude_id, limit),
    )
    out = []
    for r in rows:
        qa = db_mod.query(
            "SELECT seq, category, question, score FROM qa_items WHERE interview_id=%s ORDER BY seq",
            (r["id"],),
        )
        item = dict(r)
        item["interview_date"] = str(r["interview_date"]) if r.get("interview_date") else None
        item["qa_items"] = qa
        out.append(item)
    return out


def _set_stage(iid: int, status: str, stage: str) -> None:
    db_mod.execute(
        "UPDATE interviews SET status=%s, stage=%s, updated_at=NOW() WHERE id=%s",
        (status, stage, iid),
    )


def _run_pipeline(iid: int, email: str, need_asr: bool) -> None:
    """异步串行流水线：转写 → 整理 → 分析。任一步失败标 failed。"""
    try:
        row = db_mod.query_one(
            "SELECT company, role_title, round_name, transcript, audio_oid, audio_name, audio_type "
            "FROM interviews WHERE id=%s",
            (iid,),
        )
        if not row:
            return

        # ---- 阶段 1：ASR 转写 ----
        transcript = (row.get("transcript") or "").strip()
        if need_asr and row.get("audio_oid"):
            _set_stage(iid, "transcribing", "转写中：正在识别录音…")
            audio_bytes = db_mod.read_audio(row["audio_oid"])
            asr_result = asr_mod.transcribe(
                audio_bytes,
                filename=row.get("audio_name") or "audio.m4a",
                content_type=row.get("audio_type"),
            )
            raw_text = asr_result["text"] or ""
            segments = asr_result.get("segments") or []
            duration = int(asr_result.get("duration") or 0) or None

            # 存原始转写与 segments
            db_mod.execute(
                """
                UPDATE interviews
                   SET raw_transcript=%s, segments=%s::jsonb,
                       duration_sec=COALESCE(duration_sec, %s), updated_at=NOW()
                 WHERE id=%s
                """,
                (raw_text, json.dumps(segments, ensure_ascii=False), duration, iid),
            )

            # ---- 阶段 2：整理成对话稿（分角色） ----
            if ai_mod.ai_available() and raw_text:
                _set_stage(iid, "polishing", "整理对话稿：识别说话人角色…")
                try:
                    transcript = ai_mod.polish_transcript(raw_text)
                except Exception as e:
                    # polish 失败不阻塞主流程，退回原始文本
                    transcript = raw_text
                    print(f"[warn] polish failed for {iid}: {e}")
            else:
                transcript = raw_text

            db_mod.execute(
                "UPDATE interviews SET transcript=%s, updated_at=NOW() WHERE id=%s",
                (transcript, iid),
            )

        if not transcript.strip():
            raise RuntimeError("没有可分析的转写文本")

        # ---- 阶段 3：AI 分析 ----
        _set_stage(iid, "analyzing", "AI 复盘中：逐题评分与建议…")
        history = _load_history(email, iid)
        result = ai_mod.analyze_interview(
            transcript=transcript,
            company=row.get("company") or "",
            role_title=row.get("role_title") or "",
            round_name=row.get("round_name") or "",
            history=history,
        )
        qa_items = result.pop("qa_items", []) or []

        score = result.get("overall_score")
        try:
            score = max(0, min(100, int(score)))
        except Exception:
            score = None

        db_mod.execute(
            """
            UPDATE interviews
               SET status='done', stage=NULL, overall_score=%s, analysis=%s::jsonb,
                   error_msg=NULL, updated_at=NOW()
             WHERE id=%s
            """,
            (score, json.dumps(result, ensure_ascii=False), iid),
        )
        db_mod.execute("DELETE FROM qa_items WHERE interview_id=%s", (iid,))
        for i, q in enumerate(qa_items, start=1):
            try:
                qscore = int(q.get("score"))
                qscore = max(0, min(10, qscore))
            except Exception:
                qscore = None
            db_mod.execute(
                """
                INSERT INTO qa_items
                  (interview_id, seq, category, question, answer_digest,
                   score, strengths, issues, better_answer)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    iid, q.get("seq") or i, (q.get("category") or "其他")[:40],
                    q.get("question"), q.get("answer_digest"), qscore,
                    q.get("strengths"), q.get("issues"), q.get("better_answer"),
                ),
            )
    except Exception as e:
        msg = f"{e}"[:800]
        traceback.print_exc()
        try:
            db_mod.execute(
                "UPDATE interviews SET status='failed', stage=NULL, error_msg=%s, updated_at=NOW() WHERE id=%s",
                (msg, iid),
            )
        except Exception:
            pass


# --------------------------- 成长趋势 ---------------------------

@app.get("/api/trend")
def trend(
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
) -> JSONResponse:
    user = _require_user(x_user_email)
    _need_db()
    rows = db_mod.query(
        """
        SELECT id, company, role_title, round_name, interview_date,
               overall_score, outcome, created_at
        FROM interviews
        WHERE user_email=%s AND status='done' AND overall_score IS NOT NULL
        ORDER BY COALESCE(interview_date, created_at::date), id
        """,
        (user["email"],),
    )
    cats = db_mod.query(
        """
        SELECT q.category, COUNT(*) AS cnt, ROUND(AVG(q.score)::numeric, 1) AS avg_score,
               MIN(q.score) AS min_score
        FROM qa_items q JOIN interviews i ON i.id = q.interview_id
        WHERE i.user_email=%s AND q.score IS NOT NULL
        GROUP BY q.category ORDER BY avg_score ASC
        """,
        (user["email"],),
    )
    weak = db_mod.query(
        """
        SELECT q.category, q.question, q.score, q.issues, i.company, i.interview_date, i.id AS iid
        FROM qa_items q JOIN interviews i ON i.id = q.interview_id
        WHERE i.user_email=%s AND q.score IS NOT NULL AND q.score <= 6
        ORDER BY q.score ASC, i.created_at DESC LIMIT 12
        """,
        (user["email"],),
    )
    return JSONResponse(
        {
            "series": [
                {
                    "id": r["id"],
                    "label": (r.get("company") or "未命名")
                    + (f"·{r['round_name']}" if r.get("round_name") else ""),
                    "date": str(r["interview_date"]) if r.get("interview_date")
                    else r["created_at"].strftime("%Y-%m-%d"),
                    "score": r["overall_score"],
                    "outcome": r.get("outcome"),
                }
                for r in rows
            ],
            "categories": [
                {
                    "category": c["category"],
                    "count": int(c["cnt"]),
                    "avgScore": float(c["avg_score"]) if c["avg_score"] is not None else None,
                    "minScore": c["min_score"],
                }
                for c in cats
            ],
            "weakQuestions": [
                {
                    "interviewId": w["iid"],
                    "category": w["category"],
                    "question": w["question"],
                    "score": w["score"],
                    "issues": w["issues"],
                    "company": w.get("company"),
                    "date": str(w["interview_date"]) if w.get("interview_date") else None,
                }
                for w in weak
            ],
        }
    )
