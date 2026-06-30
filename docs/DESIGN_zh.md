# DataFlow-Agent 设计理念

> 一句话:**让大模型在沙箱里"自己动手解题",把解题过程录下来、打分、筛选,变成训练 Agent 的高质量数据。**

这份文档讲清楚三件事:我们为什么做这个、它怎么设计的、能用在哪些场景。不需要先读代码也能看懂。

---

## 1. 我们在解决什么问题?

要训练一个会用工具的 Agent(会搜索、会查数据库、会读文档……),最缺的就是**轨迹数据(trajectory)**:

```
任务  →  [想法 → 调用工具 → 看到结果]  →  [想法 → 调用工具 → 看到结果]  →  ……  →  最终答案
```

这种数据人工标太贵、太慢。于是我们让**大模型自己去沙箱里探索**,自动把这些轨迹生产出来。

但"能生产"还不够。直接收集的轨迹质量参差不齐——有的走了弯路、有的工具用错、有的根本没答对。**如果把垃圾数据喂给模型,只会越训越差。**

所以 DataFlow-Agent 的核心主张是:

> **不只是"采集"轨迹,而是"采集 → 打分 → 筛选",只留下高质量的那部分。**

这就是我们和"只会收集轨迹的沙箱"最大的区别。

---

## 2. 核心设计:一条流水线,五个积木

我们把整个过程拆成五个独立的算子(operator),像积木一样拼起来:

```
                    ┌─────────────┐
   任务列表  ───►   │  生成器      │  让模型在沙箱里探索,产出轨迹
                    │ Generator   │
                    └──────┬──────┘
                           │ 轨迹
                           ▼
                    ┌─────────────┐
                    │  评估器      │  大模型当裁判,给每条轨迹打分(0~1)
                    │ Evaluator   │
                    └──────┬──────┘
                           │ 轨迹 + 分数
                           ▼
                    ┌─────────────┐
                    │  过滤器      │  按规则丢掉不合格的(不花钱、不调模型)
                    │ Filter      │
                    └──────┬──────┘
                           │ 高质量轨迹
                           ▼
                    ┌─────────────┐
                    │  修复器      │  对低分/失败的轨迹"带着诊断重做一次"
                    │ Refiner     │  救回来,而不是直接扔掉
                    └──────┬──────┘
                           │
                           ▼
                    高质量轨迹数据集
```

> 注:过滤器(扔掉)和修复器(救回)是两种处理低质量轨迹的策略,可按需选用——
> 想要又快又纯净就用过滤器,想尽量不浪费数据就用修复器,二者也能串起来用。

### 五个算子分别做什么

| 算子 | 类型 | 干什么 | 要点 |
|---|---|---|---|
| **AgentExploreGenerator** | 生成 | 让模型一步步"想 → 调工具 → 看结果",直到给出答案 | 产出**一条**线性轨迹 |
| **AgentExploreTreeGenerator** | 生成 | 每一步**同时尝试多个不同动作**,展开成一棵"探索树" | 产出**多条**候选路径,还能做正负样本对比 |
| **TrajectoryQualityEvaluator** | 评估 | 大模型当裁判,从 4 个维度打分:目标达成 / 效率 / 连贯性 / 工具使用 | 给出 `overall` 总分 + 评语 |
| **TrajectoryFilter** | 过滤 | 用**确定性规则**筛掉坏轨迹(没成功、答案为空、工具用错、死循环……) | **不调大模型**,又快又稳定、可复现 |
| **TrajectoryRefiner** | 修复 | 对失败/低分的轨迹,带上"上次哪里错了"的诊断**重新探索一次**,把它救回来 | **只修坏的**(好的原样放行,不花钱);自己不评分,交给下游重评择优 |

> 💡 **为什么过滤器不用大模型?**
> 因为很多"坏"是显而易见的(比如根本没给出答案)。先用便宜的规则过滤器把明显的垃圾扔掉,再用昂贵的大模型裁判去评估剩下的——**省钱、省时间**。

> 💡 **为什么要有修复器?**
> 过滤器直接丢掉低分轨迹有点浪费——有些只是"中途走错一步"。修复器给它们第二次机会:
> 把上一次的失败诊断(没给答案?用错工具?死循环?)塞进提示里,让模型避开老错误再做一遍。
> 这样补齐了"**生成 → 评估 → 过滤 → 修复**"的完整闭环,提升数据利用率。

---

## 3. 最关键的设计:沙箱可插拔,零耦合

这是整个项目最值得讲的设计点。

**算子永远不知道自己连的是哪个沙箱。** 它只依赖两个抽象接口:

- `LLMServingABC` —— 负责"想下一步做什么 / 当裁判打分"(任何大模型 API 都行)
- `SandboxClientABC` —— 负责"真正去执行工具调用"(任何沙箱后端都行)

```
        ┌───────────────────────────────┐
        │   四个算子 (业务逻辑)            │
        │   只认两个抽象接口,不认具体实现   │
        └───────┬───────────────┬────────┘
                │               │
        ┌───────▼──────┐  ┌─────▼─────────┐
        │ LLMServingABC│  │SandboxClientABC│
        └───────┬──────┘  └─────┬─────────┘
                │               │
         任意大模型API      ┌────┴────┬─────────┬──────────┐
         (GPT/Claude/…)   Mock沙箱  AgentFlow沙箱  你自己的沙箱
                          (离线测试)  (HTTP通信)   (写个子类即可)
```

**好处很直接:**

- 想换个沙箱?**写一个 `SandboxClientABC` 的子类就行,四个算子一行都不用改。**
- 我们和 AgentFlow 沙箱的通信**只走 HTTP**(普通 `requests`),不 import 任何 AgentFlow 的 SDK,彻底解耦。
- 测试时用 `MockSandboxClient`(纯内存、不联网、不要 GPU、不要 API key),CI 里就能把整条流水线跑通。

我们内置了两个后端,你也可以加自己的:

| 后端 | 用途 |
|---|---|
| `MockSandboxClient` | 离线开发 / 测试,零依赖 |
| `AgentFlowSandboxClient` | 连真实 AgentFlow 沙箱(HTTP 协议) |
| **你自己的** | 实现 `list_tools` + `execute` 两个方法即可 |

---

## 4. 它和 DataFlow / AgentFlow 是什么关系?

OpenDCAI 这三个项目是一套组合拳:

```
   DataFlow  (底座:数据流水线平台,提供算子基类/大模型服务/存储/注册表)
      ▲                                          ▲
      │ 复用基类                                  │ 通过 HTTP 调工具
      │                                          │
  DataFlow-Agent  ──────────────────────►  AgentFlow 沙箱
  (本项目:给轨迹打分、筛选)                  (多环境沙箱:采集轨迹)
```

- **DataFlow**:成熟的数据处理平台(已发布到 PyPI:`open-dataflow`)。我们的算子直接挂到它的注册表里,像内置算子一样按名字调用。
- **AgentFlow**:多环境 Agent 沙箱(RAG / 文档 / 深度搜索 / GUI / Text2SQL / 数据分析……),负责"采集"。
- **DataFlow-Agent(本项目)**:**桥梁 + 增值**。它把 DataFlow 的能力延伸到 Agent 领域,并补上 AgentFlow 缺的那一环——**质量评估与筛选**。

> 一句话总结分工:**AgentFlow 负责"采",DataFlow-Agent 负责"筛",DataFlow 提供"地基"。**

---

## 5. 能覆盖哪些场景?

我们是面向**文本 / 结构化领域**的探索器——只要工具的返回结果是文字或 JSON,就能用。

### ✅ 支持的场景

| 场景 | 典型工具 | 例子 |
|---|---|---|
| **Coding / Working Agent** | `read_file`, `write_file`, `run_python`, `run_tests`, `run_shell` | "修复这个 bug,让测试通过" → 改文件 → 跑 pytest 验证 |
| **Web 搜索** | `web-search`, `web-visit` | "查一下某事件的最新进展" |
| **RAG 检索** | `rag-search` | "从知识库里找答案" |
| **Text2SQL** | `get_schema`, `execute` | "每个城市有多少客户?" → 自动写 SQL 查库 |
| **文档问答** | `doc-search`, `doc-read` | "这份合同的违约条款是什么?" |
| **数据分析** | `read_csv`, `run_python` | "分析这个 CSV,算出月度趋势" |

> 🛠️ **Coding Agent 已内置**:`CodingSandboxClient` 给智能体一个**真实隔离的工作目录**,
> 带文件读写 + 跑 Python + 跑 pytest + shell 工具(路径锁在 workspace 内、命令带超时、可关 shell)。
> 真实模型实测能自主完成 `看目录 → 读代码 → 改 bug → 跑测试通过 → 收尾` 的完整闭环。

### ❌ 暂不支持的场景

| 场景 | 为什么 |
|---|---|
| **GUI / 虚拟机操作** | 这类工具返回的是**截图(图片)**,而我们的循环是把"观察结果"当文字喂回给模型的,大模型服务接口里也没有图像通道。 |

> 多模态(图像观察)的探索器是**未来工作**。不过底层的沙箱通信层本身是领域无关的,加多模态时不用推倒重来。

---

## 6. 上手有多简单?

**装好就能跑离线 demo(零依赖、不要网络、不要 API key):**

```bash
pip install open-dataflow      # 提供算子基类 / 大模型服务 / 存储
pip install -e .               # 安装本项目

# 跑离线流水线(Mock 沙箱 + 脚本化模型)
python examples/agentic_explore/run_mock_pipeline.py

# 跑离线测试套件(21 个测试,无网络/无 GPU/无 API key)
pytest test/test_agentic_explore.py -v
```

**接真实模型也只要几行(把 Mock 换成真实组件):**

```python
import dataflow_agent                                   # 导入即自动注册四个算子
from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage
from dataflow_agent import AgentExploreGenerator, AgentFlowSandboxClient

storage = FileStorage(first_entry_file_name="queries.jsonl", cache_path="./cache")
llm     = APILLMServing_request(api_url="https://.../v1/chat/completions", model_name="...")
sandbox = AgentFlowSandboxClient(base_url="http://127.0.0.1:18890", domain="text2sql")

op = AgentExploreGenerator(llm_serving=llm, sandbox=sandbox, domain="text2sql", max_steps=10)
op.run(storage.step(), input_key="query", output_key="trajectory")
```

每条产出的轨迹长这样:

```json
{
  "task": "每个城市有多少客户?",
  "steps": [
    {"thought": "先看表结构", "action": {"tool": "get_schema", "args": {...}}, "observation": {...}},
    {"thought": "写SQL查询", "action": {"tool": "execute", "args": {...}}, "observation": {...}}
  ],
  "final_answer": "北京2个,上海1个",
  "num_steps": 2,
  "success": true
}
```

---

## 7. 设计原则速记

如果只记三句话:

1. **采集 ≠ 数据。** 价值在于打分、筛选和修复,只留(或救回)高质量轨迹。
2. **算子不认沙箱。** 一切通过抽象接口解耦,换后端不改业务代码。
3. **便宜的先上。** 规则过滤器(不花钱)打头阵,大模型裁判/修复(花钱)殿后,且只对需要的轨迹动用。

---

## 8. 路线图(未来工作)

- **多模态探索器**:支持 GUI / 虚拟机的图像观察(目前只支持文本/结构化领域)。
- **偏好数据导出**:从探索树里取"最好 vs 最差"的兄弟路径,产出 DPO 式的偏好训练对。

> ✅ **已实现**:`生成 → 评估 → 过滤 → 修复` 闭环已完整落地(`TrajectoryRefiner`,见上文第 2 节)。
