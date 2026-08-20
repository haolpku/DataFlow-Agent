# DataFlow-MM-Agent

`dataflow-mm-agent` 是 DataFlow-MM 的独立多模态 Agent 扩展包，提供受控环境
中的 Agent rollout、轨迹处理、环境插件注册以及模型服务适配能力。

- 发行包名：`dataflow-mm-agent`
- Python import 名：`dataflow_mm_agent`
- Python：`>=3.10, <4`
- License：Apache-2.0

## 包含与不包含的内容

本包包含：

- 多模态消息、工具、任务、环境和轨迹契约；
- linear rollout 与 tree exploration runtime；
- `finish`、可选 workspace host tools；
- OpenAI-compatible 和 Gemini/Kigress serving；
- JSON Task/Trajectory 存储；
- Generate、Judge、Filter、Refine、Select、Replay Verify 算子；
- 外部 Env 包的注册与发现机制。

`dataflow_mm_agent/` 核心库不包含：

- 具体环境实现和任务 JSON；
- API key、`.env.local` 或本地模型服务。

具体环境应作为独立 Env 包安装，或由业务代码显式注册。仓库中的
`examples/pixel_games_baseline/` 仅作为可直接运行的接入示例，不会被核心库
自动导入或注册。

## 从 GitHub 安装

先确保 `open-dataflow-mm` 可以从当前 Python 索引安装；如果它不在索引中，
需要先单独安装其 wheel 或源码包。

克隆仓库并安装：

```bash
git clone https://github.com/YOUR_ORG/dataflow-mm-agent.git
cd dataflow-mm-agent
python -m pip install .
```

也可以直接通过 Git URL 安装：

```bash
python -m pip install \
  "git+https://github.com/YOUR_ORG/dataflow-mm-agent.git@main"
```

检查安装结果：

```bash
python - <<'PY'
import dataflow_mm_agent
from dataflow.utils.registry import OPERATOR_REGISTRY

print(dataflow_mm_agent.__version__)
print(sorted(
    name for name in OPERATOR_REGISTRY.keys()
    if name.startswith("AgentMM")
))
PY
```

导入 `dataflow_mm_agent` 时会向 DataFlow-MM 的 `OPERATOR_REGISTRY` 注册：

- `AgentMMExploreGenerator`
- `AgentMMExploreTreeGenerator`
- `AgentMMTrajectoryQualityEvaluator`
- `AgentMMTrajectoryFilter`
- `AgentMMTrajectoryRefiner`
- `AgentMMTrajectorySelector`
- `AgentMMTrajectoryVerifier`

## 快速运行 PixelGames baseline

仓库内置一个隔离的完整示例，包含 PixelGames Env、任务 JSON、环境 verifier
以及 Generate → Replay Verify → Judge → Filter/Refine → Select pipeline：

```bash
# 在仓库根目录安装当前源码
python -m pip install -e .

cd examples/pixel_games_baseline
cp .env.example .env.local
# 编辑 .env.local，填入 OPENAI_BASE_URL、OPENAI_API_KEY 和 OPENAI_MODEL

# 推荐为每次运行指定一个新目录
python baseline_pipeline.py \
  --output-dir runs/first_run \
  --max-workers 1 \
  --max-tokens 8192 \
  --timeout 300
```

接口需要兼容 OpenAI Chat Completions，并支持 vision `image_url` 内容块。不传
`--max-steps` 时，每个 task 使用其 JSON 中的 `episode_config.max_steps`；传入该参数
则统一覆盖所有任务的 budget。最终选择结果位于
`runs/first_run/10_selected.jsonl`，其余 JSONL 保留 pipeline 的各个中间阶段。

完整的输入格式、配置项、运行参数和 01–10 阶段说明见
[`examples/pixel_games_baseline/README.md`](examples/pixel_games_baseline/README.md)。
也可以直接打开随仓库发布的
[`pixelgames_baseline_report.html`](examples/pixel_games_baseline/pixelgames_baseline_report.html)
查看四任务完整运行、中文 Judge rubric 和多模态 Refine 示例。
另有一份
[`why_we_need_verifier_task0003.html`](examples/pixel_games_baseline/why_we_need_verifier_task0003.html)
展示 VLM Judge false positive，解释为什么仍需 deterministic verifier。

## Serving 配置

### OpenAI-compatible

适用于 OpenAI API、vLLM、SGLang 或其他 Chat Completions 兼容服务：

```bash
export SERVING_BACKEND=openai
export API_URL=http://127.0.0.1:8000/v1
export MODEL=your-model-id
export DF_API_KEY=EMPTY
```

### Gemini/Kigress

适用于 Gemini `generateContent` 风格的 Kigress 网关：

```bash
export SERVING_BACKEND=gemini
export API_URL=http://your-gateway/your-route
export MODEL=your-gemini-model-id
export KIGRESS_API_KEY=your-consumer-key
export KIGRESS_USER_KEY=your-route-key
export KIGRESS_LLM_MODEL=your-gemini-model-id
export KIGRESS_BIZ_SCENE=offline
```

`KIGRESS_LLM_MODEL` 留空时默认使用 `MODEL`。不要把真实凭据提交到 GitHub。

两种后端使用同一个调用接口：

```python
from dataflow_mm_agent import Message, create_model_serving_from_env

serving = create_model_serving_from_env(timeout=120, max_tokens=128)
response = serving.generate((Message.text("user", "ping"),))
print(response)
```

## 接入环境包

Env 包需要提供一个无参数 `register()` 函数，在其中注册完整的
`EnvironmentBundle`：

```python
from dataflow_mm_agent.env import EnvironmentBundle, register_environment


def register() -> None:
    register_environment(EnvironmentBundle(
        spec=ENV_SPEC,
        env_factory=MyEnv,
        task_provider=MY_TASK_PROVIDER,
        verifier=MY_VERIFIER,
    ))
```

推荐由 Env 包通过 entry point 自动暴露注册函数：

```toml
[project.entry-points."dataflow_mm_agent.environments"]
my_env = "my_env_package:register"
```

安装 Env 包后，首次按 `env_id` 查询时会自动发现插件：

```python
from dataflow_mm_agent.env import get_environment_bundle, load_scenario

bundle = get_environment_bundle("my_env")
scenario = load_scenario("my_env", "task0001")
```

源码开发阶段也可以显式加载模块：

```python
from dataflow_mm_agent.env import load_environment_plugins

load_environment_plugins(modules=("my_env_package",))
```

或设置逗号分隔的 `DATAFLOW_MM_AGENT_ENV_PLUGINS` 环境变量。

## Rollout 与工具策略

`finish` 是 runtime 固有工具，始终存在并校验非空 `answer`。两个 exploration
generator 默认不提供 workspace host tools，初始化算子时可以显式启用：

```python
from dataflow_mm_agent import (
    AgentMMExploreGenerator,
    AgentMMExploreTreeGenerator,
)

linear = AgentMMExploreGenerator(
    serving=serving,
    include_host_tools=False,
)

tree = AgentMMExploreTreeGenerator(
    serving=serving,
    include_host_tools=True,
)
```

启用后会增加 `host.list` 和 `host.open`，并将访问限制在单次 episode 的临时
workspace 内。环境自身的可调用工具始终来自 `Env.tools()`。

## 轨迹与验证

`Trajectory` 使用 message index 关联每个 Agent step 和 observation。图片以内联
base64 形式保存在 canonical message 与 JSONL 中，不持久化本地绝对路径。

`TrajectoryStore` 支持单条和批量 JSONL：

```python
from dataflow_mm_agent import TrajectoryStore

store = TrajectoryStore()
store.save(trajectory, "trajectory.jsonl")
restored = store.load("trajectory.jsonl")
```

`AgentMMTrajectoryVerifier` 会在 fresh Env 中按顺序重放轨迹工具调用，再把最终
snapshot 交给该 Env 自己的 verifier。VLM Judge 与确定性 verifier 是相互独立的
两个阶段。

`AgentMMTrajectoryQualityEvaluator` 会把每一步 observation 图片作为独立的
`ImageContent` 交给 VLM Judge；OpenAI-compatible serving 会将其投影为
`image_url` block。`AgentMMTrajectoryRefiner` 同样使用真正的多模态上下文：文字
摘要只保留工具调用和文本 observation，旧图片单独附加，默认选择最近 4 张，可用
`max_prior_images` 调整。JSONL 和 HTTP data URL 中看到 base64 是图片的存储/传输
编码，不代表图片被拼进普通文本提示词。

## 源码结构

```text
├── dataflow_mm_agent/
│   ├── contracts/             # 消息、工具、环境、任务和轨迹契约
│   ├── env/                   # Env bundle 注册和插件发现
│   ├── operators/             # DataFlow Agent-MM 算子及其共享 utils
│   ├── runtime_components/    # finish、host tools 和 rollout runtime
│   ├── serving/               # serving 接口与 provider adapters
│   └── storage/               # task/trajectory persistence
└── examples/
    └── pixel_games_baseline/  # 可运行的 Env + verifier + task + pipeline + HTML 报告
```

## 本地开发与构建

开发模式安装：

```bash
git clone https://github.com/YOUR_ORG/dataflow-mm-agent.git
cd dataflow-mm-agent
python -m pip install -e .
```

构建标准 sdist 和 wheel：

```bash
python -m pip install build
python -m build
```

GitHub 仓库应保留 `pyproject.toml`、`README.md`、`LICENSE`、`MANIFEST.in`、完整的
`dataflow_mm_agent/` 和 `examples/`。不要提交 `dist/`、`build/`、`*.egg-info/`、
`__pycache__/`、API key、运行轨迹或本地缓存。

## License

Apache License 2.0，详见 `LICENSE`。
