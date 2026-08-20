# PixelGames baseline example

这是一个可以直接运行的 Agent-MM 端到端示例。它把具体 Env 和 task 留在
`examples/`，用于演示如何接入核心包，而不会在导入 `dataflow_mm_agent` 时自动注册。

示例包含：

- PixelGames 图像网格环境和 deterministic verifier；
- 四个 materialized task（`task0001`–`task0004`）；
- Generate → Replay Verify → Judge → Filter/Refine → Select baseline；
- OpenAI-compatible 多模态模型配置模板；
- 完整运行报告和 Judge false-positive 案例。

## 1. 安装

克隆仓库后，在仓库根目录安装：

```bash
git clone https://github.com/YOUR_ORG/dataflow-mm-agent.git
cd dataflow-mm-agent
python -m pip install -e .
```

如果 `open-dataflow-mm` 不在当前 Python 索引中，需要先安装其 wheel 或源码包。

## 2. 配置 OpenAI-compatible API

进入示例目录并复制模板：

```bash
cd examples/pixel_games_baseline
cp .env.example .env.local
```

编辑 `.env.local`：

```env
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-id
```

服务必须兼容 OpenAI `/v1/chat/completions`，并支持 vision `image_url` 内容块，因为
rollout、Refiner 和 VLM Judge 都会接收图片。`OPENAI_BASE_URL` 也可以指向 vLLM、
SGLang 或其他兼容服务；本地服务无需鉴权时可填写 `OPENAI_API_KEY=EMPTY`。

进程环境变量的优先级高于 `.env.local`，因此 CI 中也可以直接注入这三个变量。
`.env.local` 已被 `.gitignore` 忽略，不要把真实凭据提交到 GitHub。

## 3. 运行四个任务

最简运行：

```bash
python baseline_pipeline.py
```

推荐为每次实验指定独立输出目录，并为视觉模型留足输出 token 和请求时间：

```bash
python baseline_pipeline.py \
  --input input.jsonl \
  --output-dir runs/first_run \
  --max-workers 1 \
  --max-tokens 8192 \
  --judge-threshold 0.6 \
  --max-selected 4 \
  --timeout 300
```

完成后命令会打印选中数量和最终文件，例如：

```text
selected 4/4 trajectories
.../runs/first_run/10_selected.jsonl
```

模型生成具有不确定性，实际选中数量不保证总是 4/4。重复实验时应使用新的
`--output-dir`；相同目录中的同名阶段 JSONL 会被覆盖。

### 运行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--input` | `input.jsonl` | 输入任务引用 JSONL |
| `--output-dir` | `runs/` | 01–10 阶段输出目录 |
| `--max-steps` | 未设置 | 未设置时读取各 task 的 `episode_config.max_steps`；设置后统一覆盖 |
| `--max-workers` | `1` | rollout、Judge 和 Refine 的并发数 |
| `--max-tokens` | `2048` | 每次模型请求的最大输出 token |
| `--judge-threshold` | `0.6` | 低于该分数的轨迹进入 Refine 分支 |
| `--max-selected` | `4` | 最终最多保留的轨迹数 |
| `--timeout` | `300` | 单次 API 请求超时秒数 |

`--max-steps` 统计 Agent action，包括 `finish`。例如 task3 自带 8-step budget，最多
执行 8 个 action；如果第 8 步没有调用 `finish`，termination 会是 `max_steps`。

## 4. 输入任务

`input.jsonl` 每行是一个公开任务引用：

```json
{"env_id":"pixel_game","task_id":"task0001"}
{"env_id":"pixel_game","task_id":"task0002"}
{"env_id":"pixel_game","task_id":"task0003"}
{"env_id":"pixel_game","task_id":"task0004"}
```

只跑一个任务时可以新建一个 JSONL，例如：

```json
{"env_id":"pixel_game","task_id":"task0003"}
```

然后运行：

```bash
python baseline_pipeline.py \
  --input input-task3.jsonl \
  --output-dir runs/task3
```

任务本体位于 `pixel_game/tasks/`。公开输入只携带 `env_id` 和 `task_id`；运行时由
`JsonTaskProvider` 载入完整 task JSON，并绑定私有 init config、verifier 和 Judge
参考信息。

## 5. Pipeline 和 run 输出

每次 run 都会写出完整中间阶段，方便检查失败发生在哪一层：

| 文件 | 阶段 | 含义 |
| --- | --- | --- |
| `01_generated.jsonl` | Generate | 模型在 live Env 中生成原始 trajectory |
| `02_verified.jsonl` | Replay Verify | 在 fresh Env 中重放 action，得到确定性任务结果 |
| `03_judged.jsonl` | Judge | VLM 根据工具调用和真实 observation 图片评分 |
| `04_qualified_branch.jsonl` | Branch | Judge 与 verifier 均满足要求的直接合格项 |
| `04_repair_branch.jsonl` | Branch | 失败或低分、需要 Refine 的候选项 |
| `05_filtered_qualified.jsonl` | Filter | 对直接合格分支做轨迹质量过滤 |
| `06_refined.jsonl` | Refine | 使用失败诊断、文本摘要和旧 observation 图片重新 rollout |
| `07_reverified.jsonl` | Replay Verify | 在 fresh Env 中验证 Refine 后的 action |
| `08_rejudged.jsonl` | Re-Judge | 再次进行 VLM 轨迹评分 |
| `09_filtered_repaired.jsonl` | Filter | 只保留修复后真正合格的轨迹 |
| `10_selected.jsonl` | Select | 最终去重、排序并限制数量后的结果 |

某个分支没有数据时，对应 JSONL 可以为空，这是正常结果。最终训练或分析通常从
`10_selected.jsonl` 开始；排查问题时按 01 → 10 的顺序查看。

## 6. 查看随仓库发布的报告

两个 HTML 都是图片直接内嵌的自包含文件，不需要 API、运行目录或网络：

- `pixelgames_baseline_report.html`：一次完整四任务运行，包含 initial/refine 聚合、
  中文 Judge rubric、多模态输入审计和 verifier 结果；
- `why_we_need_verifier_task0003.html`：模型声称完成且 Judge 给出 1.0，但 fresh-Env
  replay 仍然失败的案例，用来说明为什么 Judge 不能替代 deterministic verifier。

可以直接双击打开，也可以在仓库根目录启动静态服务器：

```bash
python -m http.server 8000 --directory examples/pixel_games_baseline
```

然后访问：

- `http://127.0.0.1:8000/pixelgames_baseline_report.html`
- `http://127.0.0.1:8000/why_we_need_verifier_task0003.html`

两份发布报告都不包含 API 地址、API key 或本机绝对路径；Verifier false-positive
专项案例还额外移除了 system prompt。
