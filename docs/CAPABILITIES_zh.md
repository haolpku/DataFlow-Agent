# DataFlow-Agent 能力清单:支持的环境与可生成的数据

> 一句话:**DataFlow-Agent 让大模型在沙箱里多步探索,产出「思考→调用工具→观察」的智能体轨迹,并经打分/过滤/修复变成高质量训练数据。** 本文列清楚:现在支持哪些环境、能生成哪些数据、以及每一项的成熟度。

配套阅读:设计理念见 [`DESIGN_zh.md`](./DESIGN_zh.md);在线数据样例见展示站 `OpenDCAI_Data` 的「Agentic 轨迹合成」分类。

---

## 一、支持的环境(Sandbox)

环境通过 `SandboxClientABC` 接入,算子对具体环境**完全不可知**。加新环境只需写一个子类(实现 `list_tools` + `execute`),五个算子一行不用改。

### ✅ 已内置、已用真实模型跑通

| 环境 | 类 | 工具 | 状态 | 典型任务 |
|---|---|---|---|---|
| **Coding / Working Agent** | `CodingSandboxClient` | `read_file` / `write_file` / `list_files` / `run_python` / `run_tests`(pytest) / `run_shell` | ✅ 真实工作区,已产出 6 条数据 | 修 bug、实现函数、写测试、重构、调试运行时错误 |
| **知识检索 / 通用探索** | `MockSandboxClient` | `search` / `finish` | ✅ 离线内置,已产出 5 条数据 | 事实问答类多步检索 |
| **远程 HTTP 多环境** | `HTTPSandboxClient` | 由远程沙箱服务端决定(Web / RAG / Text2SQL / 文档 / 数据分析 …) | ✅ 通用 HTTP 协议已对接(需连一个运行中的沙箱服务器) | 换 `domain` 字符串即可切换环境 |

### 🟢 抽象层已支持,写个子类即可接入(文本/结构化环境,零框架改动)

只要环境的工具**输入输出是文字或 JSON**,就能接。例如:

| 环境 | 接入方式 |
|---|---|
| Shell / 命令行 agent | 复用 `CodingSandboxClient` 的 `run_shell`,或写专用子类 |
| Text2SQL / 数据库 | 子类实现 `get_schema` / `execute`(有状态,加 session 生命周期) |
| Web 搜索 / RAG 检索 | 子类实现 `search` / `visit`,或走远程 HTTP 沙箱 |
| 文档问答 | 子类实现 `doc_search` / `doc_read` |
| 数据分析 | 子类实现 `read_csv` / `run_python` |
| 任意 API / MCP server / 内部工具 | 子类把调用结果包成 `ToolResult` 即可 |

### ❌ 暂不支持(框架级待办)

| 环境 | 原因 |
|---|---|
| **GUI / 虚拟机**(观察是截图/图片) | 探索循环把观察当**文字**喂回给模型,`LLMServingABC` 无图像通道。需多模态改造(路线图项) |

---

## 二、可生成的数据类型

数据由五个算子组成的流水线产出:**生成 → 评估 → 过滤 → 修复**。

### 1. 生成(Generator)

| 数据类型 | 算子 | 产出形态 | 用途 |
|---|---|---|---|
| **线性轨迹** | `AgentExploreGenerator` | 单条 `task → [thought, tool, observation]* → answer` | SFT 训练数据 |
| **分支探索树** | `AgentExploreTreeGenerator` | 一棵树 + 所有根到叶路径(展平为线性轨迹) | 多候选采样;可导出「最优 vs 最差」偏好对(路线图) |

**统一轨迹结构**:
```json
{
  "task": "...",
  "steps": [{"thought": "...", "action": {"tool": "...", "args": {...}}, "observation": ...}],
  "final_answer": "...",
  "num_steps": 3,
  "success": true
}
```

### 2. 评估(Evaluator)

| 算子 | 产出 |
|---|---|
| `TrajectoryQualityEvaluator` | LLM-as-judge 四维打分(1-5):**目标达成 / 效率 / 连贯性 / 工具使用** + 综合分 `overall`(0-1)+ 裁判评语 |

### 3. 过滤(Filter)

| 算子 | 规则(确定性、不调大模型) |
|---|---|
| `TrajectoryFilter` | 未成功 / 步数越界 / 答案为空 / 解析错误 / 幻觉工具 / 工具报错 / 重复动作死循环 |

### 4. 选择(Selector)

| 算子 | 作用 |
|---|---|
| `TrajectorySelector` | 从候选轨迹池里**确定性地选 top-N 条高质量且多样**的轨迹(无 LLM)。三维打分:深度(40)+ 信息量(30)+ 工具多样性(30),满分 100;再用**动作集合 Jaccard 相似度去重**(阈值 0.7)。两种模式:`tree`(从探索树的多条 paths 里每棵选 N 条)/ `rows`(把整表轨迹当候选池选 N 行)。 |

> 与 Filter 的区别:Filter 是"逐条判定好坏、丢掉坏的";Selector 是"从一堆里挑出最好且互不重复的 N 条"(一个 seed → 一棵树 → 选 N 条精华)。

### 5. 修复(Refiner)

| 算子 | 作用 |
|---|---|
| `TrajectoryRefiner` | 对失败/低分轨迹,带「上次失败诊断」重新探索一次,救回而非丢弃;好轨迹原样放行(不花钱) |

---

## 三、目前已实际产出的数据(真实模型,非模拟)

模型:`claude-haiku-4-5`,经 OpenAI 兼容网关。

### 已上线展示站(`OpenDCAI_Data` → 「Agentic 轨迹合成」,共 12 条)

| 子类 | 条数 | 说明 |
|---|---|---|
| 知识检索线性轨迹 | 5 | 综合分 0.95~1.0 |
| 分支探索树 | 1 | 含节点/路径统计 |
| **Coding Agent** | 6 | 真实工作区 + pytest,全部成功、质量分 1.0 |

**6 条 Coding 覆盖技能**:修 bug(fizzbuzz)、实现函数(flatten 嵌套展平)、调试运行时错误(stats 的 ZeroDivisionError,先看 traceback 再修)、写测试(palindrome)、重构(dedup O(n²)→O(n))、算法实现(roman_to_int)。

### 仓库 `cache/` 内的其他数据

| 数据 | 条数 | 用途 |
|---|---|---|
| filter 验证集 | 8 → 2 | 2 好 + 6 坏(每条命中一条过滤规则),证明过滤器 8→2 真的生效 |

---

## 四、成熟度速览

| 能力 | 状态 |
|---|---|
| Coding/Working Agent 环境 | ✅ 已产出数据 |
| 知识检索环境(Mock) | ✅ 已产出数据 |
| 远程 HTTP 多环境 | ✅ 已对接,待接真实服务端批量产数据 |
| 线性轨迹生成 | ✅ |
| 分支探索树生成 | ✅(支持 depth_threshold:深层收敛单路省算力) |
| 质量评估(四维打分) | ✅ |
| 规则过滤 | ✅ 已验证 |
| 轨迹选择(Selector) | ✅ 已实现 + 测试 |
| 轨迹修复(Refiner) | ✅ 已实现 + 测试通过,🔜 待沉淀展示样本 |
| 偏好对导出(DPO) | 🔜 路线图 |
| 多模态(GUI/VM 图像) | 🔜 路线图(需框架改造) |

---

## 五、一句话给不同读者

- **想扩环境的**:文本/结构化环境写个 `SandboxClientABC` 子类就行,五个算子不动;GUI/VM 要等多模态。
- **想要数据的**:现在能拿 Coding、知识检索、探索树三类轨迹(带质量分),接上远程 HTTP 沙箱后可扩到 Web/RAG/SQL/文档/数据分析。
- **想看效果的**:去展示站「Agentic 轨迹合成」分类,12 条真实样本,每步思考/工具/观察全可见。
