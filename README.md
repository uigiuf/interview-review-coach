# 面试复盘教练

<p align="center"><img src="cover.png" alt="cover" width="480"></p>

把每一场面试，变成下一场的弹药。

上传面试录音，服务端自动转写并生成一份严格的复盘报告：逐题评分、更优答案示范、表达习惯诊断、面试官关注点提炼，并跨场次追踪你的进步。

## 功能

- **上传即解析**：拖入录音文件（mp3/m4a/wav/webm），后台自动转写→整理对话稿→AI 复盘，页面轮询看进度
- **逐题诊断**：每个问题单独评分（0-10），指出问题必须引用原话作证据
- **更优答案示范**：用你自己已有的真实素材重写成可直接说出口的回答，不是模板套话
- **表达习惯诊断**：口头禅计数、语速节奏、啰嗦度、表达结构，外加「立刻要改的三件事」
- **面试官关注点**：从提问与追问方式推断他真正在意什么、想听但没听到的、下轮该准备什么
- **跨场次进步追踪**：从第二场起自动对比历史，标出进步项、退步项、反复出现的顽固问题
- **成长趋势看板**：综合分曲线、各类问题平均分、反复答不好的题

评分标准刻意从严（普通回答 5-6 分），因为安慰式高分对拿 offer 没用。

## 架构

```
用户上传录音
    ↓
FastAPI 保存文件（PG Large Object）→ 立即返回任务 id
    ↓（后台线程串行执行）
[1] ASR 转写      —— OpenAI Whisper 兼容协议
[2] 整理对话稿    —— Chat LLM，识别说话人角色、修正音同字错
[3] AI 复盘分析   —— Chat LLM，输出结构化 JSON 报告
    ↓
前端每 3-4 秒轮询状态，进度自动刷新
```

服务端 ASR 端点可任意替换（不改代码，改环境变量即可）：

| 端点 | 是否兼容 | 备注 |
|---|---|---|
| OpenAI Whisper API | ✅ 默认 | 25MB 上限，$0.006/分钟 |
| Groq Whisper | ✅ | 速度快很多，模型填 `whisper-large-v3`；同样 25MB |
| 自托管 whisper.cpp server | ✅ | 无 API 费用，无 25MB 限制，数据不出机器 |
| faster-whisper-server | ✅ | 支持 GPU 加速，同样自托管 |

## 快速开始

### 1. 准备 PostgreSQL

```bash
docker run -d --name interview-pg \
  -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=interview_review \
  -p 5432:5432 postgres:16
```

表结构在首次访问时自动创建。

### 2. 配置环境变量

```bash
cp .env.example .env
# 填入 ASR_API_KEY、OPENAI_API_KEY，然后 export 或用 python-dotenv 加载
```

### 3. 安装依赖并启动

```bash
pip install -r requirements.txt
uvicorn app:app --port 8000
```

浏览器打开 http://localhost:8000。

## 三种输入方式

| 方式 | 说明 |
|---|---|
| 🎧 上传录音文件 | 拖入 mp3/m4a/wav/webm，服务端 ASR 自动转写 |
| 🎙️ 现场实时录音 | 面试当场开着页面录，录完自动走同一套流水线 |
| 📝 直接粘贴文字 | 已有转写文本时最快的路径，跳过 ASR 直接进分析 |

## 说明

- 录音文件与转写文本都存在你自己的 PostgreSQL 里，只有分析请求会把转写文本发给 LLM。
- 单个录音文件受 ASR 服务限制（OpenAI/Groq 官方为 25MB）；超限请压缩或分段后再传，或改用自托管的 whisper 服务（无此限制）。
- 本仓库为个人自部署版本，由内部部署版脱敏而来。所有外部依赖均通过环境变量配置。
