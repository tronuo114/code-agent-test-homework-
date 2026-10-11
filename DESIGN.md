# CodeReview 设计文档

本文说明 CodeReview 的架构决策、关键算法与工程权衡。
建议先看 [README.md](README.md) 了解怎么用，再看本文了解**为什么这样做**。

---

## 1. 设计目标与取舍

### 1.1 三个明确目标

1. **任务单一**：只做一件事 —— 找出代码里**可能出错的缺陷**。不做风格审查、
   不做重构建议、不做代码生成。范围收窄让 Prompt 可以把约束压到很深
   （见 [第 3 节](#3-代码审查模式的-prompt-设计)）。
2. **双击即用**：交付物是一个文件夹，`CodeReview.exe` 双击就跑，
   目标机不需要装 Python、不需要 `pip install`、不需要配环境变量。
3. **可被评审**：零第三方依赖；每个非显然的决策都在代码注释与本文里写清"为什么"。

### 1.2 关键取舍

| 决策 | 选择 | 放弃的方案 | 理由 |
|------|------|-----------|------|
| 打包方式 | C# 启动器 + 精简 Python 运行时 | PyInstaller 单文件 | 构建环境**连不通 PyPI**，PyInstaller 装不上；而且 `csc.exe` 随 Windows 自带，正好够用 |
| 启动器语言 | C# 5 语法 | C# 6+ | .NET Framework 自带的 `csc.exe` 只认到 C# 5，用新语法直接编不过 |
| 运行时来源 | 复制本机已有 Python 并剔除 `site-packages` | 下载 embeddable 包 | 无外网；剔除后 164.9 MB → 34.2 MB，而本 Agent 是纯标准库的 |
| 密钥存储 | 程序目录 `config.json`（明文） | 系统凭据管理器 / 加密 | 绿色便携、可审计；加密必须引入密钥管理，反而不透明。改为**明确告知**并加进 `.gitignore` |
| 密钥校验 | 真发一次极小请求 | 只校验格式 | "存下来就算绑定成功"会让用户在第一次审查时才遇到 401。一次 `max_tokens=8` 的调用成本可忽略 |
| 工具集 | 只注册 3 个只读工具 | 复用全部 7 个工具 + 权限开关 | 审查不需要写；**没注册 = 模型看不见 = 不可能误用**，比"能调但被拒绝"强 |
| 校验失败判定 | 分成功/警告/失败三档 | 二值成功失败 | 限流 429 和网络不通**不代表密钥填错了**，判成失败会误导用户反复重输 |

---

## 2. 分层架构

```
┌──────────────────────────────────────────────────────────┐
│ review_cli.py    命令循环 · 终端渲染 · 报告保存            │  表现层
│ (cli.py          完整助手模式，保留非默认)                 │
└───────────────────────────┬──────────────────────────────┘
                            │ 只传 AgentHooks 回调
┌───────────────────────────▼──────────────────────────────┐
│ agent.py         CodeAgent.run()  —— ReAct 主循环         │  编排层
└───┬───────────────┬────────────────┬─────────────────────┘
    │               │                │
┌───▼────────┐ ┌────▼─────────┐ ┌────▼────────────────────┐
│ memory.py  │ │ tools/       │ │ llm.py                  │  能力层
│ 上下文      │ │ 注册表+工具   │ │ OpenAI 兼容客户端        │
└────────────┘ └──────────────┘ └────┬────────────────────┘
                                      │
┌──────────────┬──────────────┬───────▼──────┬─────────────┐
│ store.py     │ verify.py    │ config.py    │ errors.py   │  基础层
│ 绑定持久化    │ 密钥校验      │ 配置         │ 异常体系     │
└──────────────┘──────────────┴──────────────┴─────────────┘
```

**依赖方向始终向下**，没有任何反向 import。三个可以验证的收益：

- `agent.py` 不知道 HTTP 的存在（只知道 `llm.chat(...) -> LLMResponse`）；
- `llm.py` 不知道工具的存在（只把 `tools` 当 dict 列表透传）；
- `tools/` 不知道 LLM 的存在（只返回 `ToolResult`）。

**"换一个前端"这件事已经被验证过两次**：从 `cli.py`（通用助手）换成
`review_cli.py`（代码审查），`agent.py` 只多了一个 `system_prompt_template` 参数，
其余一行没改。

---

## 3. 代码审查模式的 Prompt 设计

这是本项目最核心的工程部分。审查质量几乎完全取决于 Prompt 把模型约束到什么程度。

### 3.1 为什么不能只说"你是一个代码审查专家"

因为那会得到一篇读后感：模型倾向于把"这段代码可以更简洁"当作发现，
把风格偏好混进缺陷列表，并且在没读懂的地方**编造**看起来专业的问题。

所以 `REVIEW_SYSTEM_PROMPT` 被写成一份**可执行的规程**，四个部分各自解决一个具体失效模式：

| 段落 | 针对的失效模式 |
|------|---------------|
| **第一原则：证据来自工具，不来自记忆** | 幻觉。明确禁止凭文件名猜内容、凭函数名猜签名；要求查不到时写"未能确认"，并直说"绝不编造" |
| **八个审查维度（按优先级排序）** | 关注点发散。给出一份有优先级清单，把模型注意力压到"会出错"的地方 |
| **工作方法（先定范围 / 由粗到细 / 交叉验证 / 并行取数）** | 低效遍历。防止一上来把整个仓库读一遍，或在同一个地方反复打转 |
| **输出格式（强制）+ 硬性要求** | 报告不可用。强制 `文件:行号`、强制说清"什么情况下会出错" |

### 3.2 八个审查维度

按"造成实际损失的可能性"从高到低排列：

1. **正确性缺陷** —— 逻辑写反、条件分支漏掉、off-by-one、`=` vs `==`、复制粘贴忘改变量名
2. **空值与类型** —— None 被直接使用、字典取键未判存在、整数除法、浮点 `==`
3. **异常处理** —— 裸 `except:` 吞异常、`finally` 里 return 覆盖返回值
4. **资源与生命周期** —— 文件/连接/锁未关闭、提前 return 跳过释放
5. **并发与状态** —— 共享可变状态无保护、TOCTOU、迭代中修改集合
6. **安全** —— 命令注入、路径穿越、不安全反序列化、硬编码密钥
7. **可复现性与环境** —— 编码未指定、路径分隔符硬编码、时区、随机数未设种子
8. **会导致错误的性能问题** —— 无终止循环、递归无终止、循环里做 O(n²)

**为什么把性能排在最后**：单纯的"这里可以更快"不是缺陷。
只有"会导致内存爆掉 / 跑不完 / 结果错"的性能问题才值得占用报告篇幅。

### 3.3 明确排除风格问题

规程里单列了一条：

> **不要报告**：命名风格、缩进空格、注释多少、类型注解缺失、
> "建议用列表推导式"之类的偏好问题。这些会让真正的缺陷被淹没。

这一条的效果是：报告条目数下降，但每条都更值得看。

### 3.4 强制格式的三个作用

输出格式规定为 `结论 → 🔴严重 / 🟠中等 / 🟡轻微 → 未确认项`，每条含
`位置 / 问题 / 影响 / 建议`。这不只是为了好看：

1. **`文件:行号` 是防幻觉的硬约束** —— 模型必须真的读到那一行才能给出行号。
   `read_file` 返回的内容是带行号的，编造的行号与真实内容对不上，用户一眼能识破。
2. **"影响：什么情况下会出错"逼模型自证** —— 说不清触发条件的，多半不是真缺陷。
   规程里写了"说不清就降级为轻微或干脆不报"。
3. **"未确认项"给了模型一个体面的出口** —— 没有这个栏目，模型在"信息不足"时
   会倾向于猜；有了它，"我无法确认 X，需要 Y"就是合法输出。

### 3.5 允许说"没问题"

规程最后一条：

> **没有发现问题就如实说"未发现明显缺陷"**，并说明你检查了哪些文件、覆盖了哪些维度。
> 为了显得有产出而编造问题，是最严重的失败。

这是最重要的一条。LLM 有强烈的"必须给出点东西"的倾向，
不明确禁止它就会为了填满报告而虚构问题 —— 对一个审查工具来说，这比漏报更有害，
因为它消耗的是用户的信任。

### 3.6 与通用助手 Prompt 的复用机制

两个 Prompt 都是 `str.format` 模板，占位符完全相同：

```
os_name · workspace · write_mode · tool_catalog
```

因此 `CodeAgent` 只需一个参数：

```python
CodeAgent(..., system_prompt_template=REVIEW_SYSTEM_PROMPT)
```

`build_system_prompt()` 里就一句：`(template or SYSTEM_PROMPT_TEMPLATE).format(...)`。
换 Prompt 不需要动循环、不需要动工具、不需要动 LLM 客户端。

---

## 4. Agent 主循环

### 4.1 状态机

```
                    ┌──────────────┐
   run(user_input)  │ 追加 user 消息 │
   ────────────────▶└──────┬───────┘
                           ▼
                 ┌─────────────────────┐
             ┌──▶│ step < max_steps ?  │──否──┐
             │   └─────────┬───────────┘      │
             │             │ 是               │
             │             ▼                  │
             │   ┌───────────────────┐        │
             │   │ 调用 LLM（带工具）  │        │
             │   └─────────┬─────────┘        │
             │             ▼                  │
             │      ┌──────────────┐          │
             │      │ 有 tool_calls?│          │
             │      └──┬────────┬──┘          │
             │     是  │        │ 否          │
             │         ▼        ▼             │
             │  ┌───────────┐ ┌────────────┐  │
             │  │执行工具    │ │输出最终答案 │  │
             │  │结果回灌    │ └────────────┘  │
             │  └─────┬─────┘                 │
             │        ▼                       │
             │  ┌───────────────┐             │
             └──┤注入纠偏提示    │             │
                └───────────────┘             │
                                              ▼
                                   ┌─────────────────────┐
                                   │ 追加收尾提示          │
                                   │ 再调一次 LLM（无工具）│
                                   │ 强制产出最终答案      │
                                   └─────────────────────┘
```

审查模式把 `max_steps` 默认调到 **25**（通用助手是 12）：
审查比问答需要更多的探查步骤 —— 列目录、检索、逐个读文件。

### 4.2 六道防护栏

真实使用中模型会以各种方式把事情搞砸，每道防护栏对应一种具体的翻车方式：

| # | 机制 | 防的是什么 | 实现位置 |
|---|------|-----------|---------|
| 1 | **工具结果回灌** | 工具失败导致任务中断。改成把错误当"观察结果"交回模型自纠错 | `_record_tool_result` |
| 2 | **孤儿 tool_call 补齐** | 模型给出的 `arguments` 不是合法 JSON 时，若不回一条对应的 `tool` 消息，下一轮请求会被服务端 400 拒绝 | `_handle_tool_call` 首段 |
| 3 | **重复调用抑制** | 模型陷入"用完全相同的参数反复调用"的死循环。第 3 次起拦截并说明原因 | `REPEAT_BLOCK_THRESHOLD` |
| 4 | **连续失败提醒** | 同一工具连续失败 2 次后注入提示，要求换策略 | `FAILURE_STREAK_THRESHOLD` |
| 5 | **步数上限兜底** | 用完 `max_steps` 时追加提示并以 `tools=None` 再调一次，**保证一定有输出** | `STEP_LIMIT_NUDGE` |
| 6 | **钩子异常隔离** | 渲染回调抛异常不能影响主流程 | `_safe()` |

### 4.3 为什么"解析失败也必须回一条 tool 消息"

OpenAI 协议规定：`assistant` 消息里出现的每个 `tool_calls[i].id`，
都必须在后续消息中有条 `{"role":"tool","tool_call_id":...}` 与之对应。

如果模型返回 `arguments: "{not json"`，一个"聪明"的实现可能选择丢弃这次调用。
但那样上下文里就留下了一个没有响应的 `tool_calls`，**下一轮请求必然 400**；
更糟的是模型永远不知道自己错在哪。

所以解析失败时也生成一条 `tool` 消息，内容是清晰的错误说明 + 模型原始的 `arguments`：

```python
if call.parse_error:
    result = ToolResult.failure(
        f"工具参数解析失败：{call.parse_error}\n"
        f"你返回的原始 arguments 为：{call.raw_arguments[:400]}\n"
        f"请重新以合法 JSON 对象的形式提供参数。",
        kind="tool.argument",
    )
```

---

## 5. 工具系统

### 5.1 抽象设计

```python
class Tool(ABC):
    name: str                      # 模型调用时用的名字
    description: str               # 给模型看的能力说明
    parameters: dict               # JSON Schema 的 properties 部分
    required: tuple[str, ...]      # 必填参数
    requires_write: bool = False   # 只读模式下是否直接拒绝
    mutating: bool = False         # 是否会改变状态

    def schema(self) -> dict: ...          # 导出 OpenAI 函数调用格式
    def validate(self, arguments) -> dict: # 参数校验 + 类型强转
    def execute(self, arguments) -> ToolResult:  # 校验 → 执行 → 异常兜底
    @abstractmethod
    def run(self, **kwargs) -> ToolResult: ...
```

**关键约束：`execute()` 永不抛异常**（`KeyboardInterrupt` 除外）。
任何失败都被转成 `ToolResult(ok=False, ...)` —— 对 Agent 来说
"工具失败了"是一条**信息**，不是一次**故障**。

### 5.2 审查模式的工具收窄

```python
REVIEW_TOOL_CLASSES = (ListDirTool, ReadFileTool, SearchCodeTool)
```

三个理由：

1. **任务不需要** —— 审查是"找出问题"，不是"修好它"；
2. **没注册 = 不可能误用** —— 比"能调用但被权限层拒绝"多一层保证，
   而且模型看不到写工具，也就不会在报告里说"我已经帮你改了"；
3. **更短的清单 = 更准的选择** —— 7 个工具时模型偶有选错，3 个时基本不会，
   同时省 token。

`ToolContext(allow_write=False)` 作为第二道保险：即使有人把写工具塞进注册表，
`Tool.execute()` 也会在调用 `run()` 之前拒绝。

### 5.3 参数校验：宽容强转 + 精确报错

模型传参有很强的"字符串化"倾向，校验层按 JSON Schema 做**宽容强转**：

| Schema 类型 | 接受 | 转成 |
|------------|------|------|
| `integer` | `5`, `"5"`, `5.0` | `5` |
| `boolean` | `true`, `"true"`, `"1"`, `"yes"`, `"on"` | `True` |
| `array` | `[...]`, `"a,b,c"` | `["a","b","c"]` |

同时**拒绝未知参数**并列出可接受的参数名 —— 这是发现"模型记错参数名"的最快路径。

**踩过的坑**：最初把「空字符串」也判为缺参，结果 `edit_file(new_string="")` 被拒 ——
而这是文档承诺的"删除该片段"用法。现在只把「键缺失」或「显式为 null」视为缺参：

```python
missing = [n for n in self.required
           if n not in arguments or arguments.get(n) is None]
```

### 5.4 沙箱：路径围栏

```python
def resolve(self, raw, *, purpose="访问") -> Path:
    candidate = (self.workspace / Path(raw)) if not absolute else Path(raw)
    resolved = candidate.resolve()          # ← 先解析符号链接
    if not self._is_inside(resolved):
        raise WorkspaceViolation(...)       # 带可操作提示
    return resolved
```

审查模式下 `workspace` 由**审查目标推导**：目标是目录则用它，是文件则用其父目录。
这样模型能读到该文件的兄弟文件与被调函数的定义 —— 审查需要这种"邻里上下文"。

**一个必须注意的接线细节**：交给模型的路径必须是**相对沙箱根**的，
而不是相对进程 cwd 的。否则模型按字面把 `codeagent/tools/registry.py` 传给
`read_file` 时，会在 `.../codeagent/tools/` 下面再找一层，直接报"目录不存在"。
这就是 `_relative_to()` 存在的原因，也是开发中真实踩到并修掉的一个 bug。

### 5.5 各工具的边界情况

| 工具 | 处理了的边界情况 |
|------|-----------------|
| `list_dir` | 空目录；不是目录时提示改用 `read_file`；递归跳过噪声目录并显式标注；条目数上限 |
| `read_file` | 文件不存在时**列出同级文件**帮模型改错别字；二进制拒绝；超大文件要求分段；单行过长截断；`start_line` 超界报错；UTF-8→GBK→Big5 编码嗅探；CRLF 保留 |
| `search_code` | 非法正则给出清晰报错；空 pattern 拒绝（否则匹配每一行）；跳过二进制与大文件；无结果时给出"放宽条件"的建议 |

---

## 6. 上下文记忆

### 6.1 问题

LLM 无状态，"记忆"就是把历史消息原样带上。但上下文窗口有限，对话一长必须裁剪。
**朴素做法会造成两类致命错误**：

1. **孤儿 tool 消息** —— `assistant(tool_calls)` 被丢掉，但它的 `role=tool` 结果还在，
   多数服务端直接 400；
2. **半个工具回合** —— 一次 assistant 并发调了 3 个工具，只保留了其中 2 个结果。

### 6.2 原子块裁剪算法

```
1. 把消息切成原子块：
     assistant 带 tool_calls → 与紧随其后、tool_call_id 属于它的所有 tool 消息合成一块
     其它消息各自成块
2. 从最后一块往前累加，直到超过预算
3. 反转回正序，前面拼上 system prompt
4. 若有丢弃，插入一条"失忆提示"
```

关键在第 1 步：**块内不可拆分**。这样无论从哪里截断，协议完整性都自动成立。

```python
for block in reversed(blocks):
    cost = sum(estimate_message_tokens(m) for m in block)
    if kept and used + cost > budget:
        break
    kept.append(block)
    used += cost
```

`if kept and ...` 这个条件保证**至少保留一个块** —— 否则在极端小预算下会退化成
"只剩 system prompt"，模型完全失去上下文。

### 6.3 Token 估算

不引入 `tiktoken`（额外的重依赖），改用混合启发式：

```python
ascii_count * 0.25 + wide_count * 1.0   # 再乘 1.15 安全系数
```

依据：英文约 4 字符 1 token；CJK 约 1 字符 1 token 甚至更多。
乘安全系数是刻意**高估** —— 低估会导致真正的超限报错，高估只是提前裁剪一点历史。

---

## 7. LLM 客户端

### 7.1 重试策略

```python
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}
```

| 错误类型 | 可重试 | 退避 |
|---------|--------|------|
| 429 / 5xx | ✅ | 优先用响应体里的 `retry_after`，否则指数退避 |
| 超时 / 连接失败 | ✅ | 指数退避 + 抖动 |
| 400 / 401 / 403 / 404 / 422 | ❌ | 立即抛出并翻译成中文提示 |

退避公式：`min(0.8 * 2^(n-1) + random(0, 0.4), 20)` 秒。
抖动是为了避免多个会话同时重试形成同步冲击。

### 7.2 流式与降级

两个不显然的点：

**(1) 工具调用是分片到达的。** 必须按 `index` 合并增量，
而且**工具名本身也可能被切开**（`"read_"` + `"file"`）。
最初写成覆盖赋值导致工具名变成 `"file"` —— 测试抓到了这个 bug：

```python
if function.get("name"):
    slot["name"] += function["name"]     # 累加，不是覆盖
```

**(2) 降级必须判断"是否已经输出过内容"。** 流式失败时自动回退非流式，
但如果已经吐了一部分文本再回退，用户会看到重复内容：

```python
except LLMError as exc:
    if getattr(exc, "_partial_output", False):
        raise                      # 已经输出了，不能重放
    payload["stream"] = False
    return self._chat_once(payload)
```

### 7.3 可测试性

HTTP 传输层被抽象成一个 Protocol：

```python
class Transport(Protocol):
    def post(self, url, headers, payload, timeout, stream) -> Any: ...
```

生产用 `UrllibTransport`，测试注入 `FakeTransport`。
因此重试、超时、SSE 解析、降级这些逻辑**全部可以离线精确测试**，
2400 多行测试代码里没有一个真实网络请求。

`verify_binding()` 也接受可选的 `transport` 参数 —— 否则"401/403/429/5xx 分别怎么归类"
就只能靠真实网络碰运气覆盖。

---

## 8. 绑定的持久化与校验

### 8.1 存放位置的降级链

```
1. <exe 目录>\config.json                ← 首选，绿色便携
2. %USERPROFILE%\.codereview\config.json  ← 程序目录不可写时
```

程序目录不可写是完全可能的（放到 `Program Files`、只读介质）。
`ConfigStore.path()` 用**实际试着写一个探测文件**来判断可写性，
而不是查 ACL —— 后者在 Windows 上又长又不可靠。

`save()` 依次尝试所有候选位置，全部失败才返回错误，并把失败原因交给调用方展示。
**宁可换个地方存，也不要让用户白输一次密钥。**

### 8.2 密钥校验的三档分类

绑定必须立刻给出结论，所以要真的发一次请求。但"成功/失败"二值是不够的：

| 档位 | 触发 | 是否保存 | 为什么这样定 |
|------|------|---------|-------------|
| `ok` | HTTP 2xx | 是，标记已校验 | —— |
| `warn` | 429 / 5xx / 超时 / 网络不通 | 是，标记未验证 | **这些不代表密钥填错了**。判成失败会让用户反复重输一个本来正确的密钥 |
| `fail` | 401 / 402 / 403 / 404 / 400 | 是，标记失败 | 明确不可用，但仍保存（避免用户重输），并给出可操作的排查建议 |

`ok` 与 `warn` 都算"绑定成功"（`VerifyResult.ok`），只有 `ok` 才算"已校验"（`verified`）。
这两个属性分开，是为了让界面能区分"能用了"和"确认过了"。

失败时把**服务端原文**一起打出来，并翻译成行动建议：

```
✖ 绑定失败：API Key 无效或已被停用
  服务端返回：{"error":{"message":"Authentication Fails, Your api key: ****0000 is invalid", ...}}
  提示：可先用 url / model 指令确认端点和模型名，再重新执行 api。
```

### 8.3 参数变更失效

`model` / `url` 一改，之前的校验结论立刻作废（`verified=False`），
并提示重新执行 `api`。否则界面会显示"已校验通过"，而实际上换了个端点根本没验过。

---

## 9. 免安装整包的构建链路

### 9.1 为什么不是 PyInstaller

最常见的做法是 PyInstaller 打成单文件 exe。本项目的构建环境**连不通 PyPI**
（`pip install` 挂起五分钟无响应），这条路走不通。

改用 Windows 自带的 `csc.exe`（.NET Framework 4.8 的 C# 编译器）：

```
csc /target:exe /codepage:65001 /out:CodeReview.exe launcher\CodeReview.cs
```

零下载、零依赖，产出一个 **7.5 KB** 的真 PE 可执行文件。

两个必须注意的细节：

- **`/codepage:65001`** —— 否则 csc 按系统 ANSI 代码页读源码，里面的中文提示会变乱码。
  （`.ps1` 那边是另一个坑：Windows PowerShell 5.1 读**无 BOM** 的 UTF-8 脚本同样会按
  GBK 解析，导致中文语法错误。所以 `build.ps1` 存成了 **UTF-8 with BOM**。）
- **只能用 C# 5 语法** —— 老 csc 不认字符串插值 `$"..."`、表达式体成员等新语法。

### 9.2 启动器做什么

```
找 main.py（缺 → 友好报错 + 暂停）
找解释器：<exe>\runtime\python.exe  →  PATH 里的 python.exe
拼命令行（按 Windows 引号规则逐字符转义）
继承当前目录启动（用户在哪个项目里执行，就审查哪个项目）
透传退出码
```

三个不显然的处理：

1. **按体积排掉商店存根** —— Microsoft Store 会放一个 0 字节的 `python.exe` 占位，
   `File.Exists` 对它返回 `true`，执行它只会弹应用商店。所以判据是"存在且大于 4 KB"。
2. **只在非零退出时暂停** —— 双击启动的窗口会在进程退出后立刻消失，
   出错信息看不到。但如果是正常 `exit`，窗口就该关掉；
   在终端里跑时也不该无端卡住。所以条件是"非零退出 且 stdin/stdout 都不是重定向"。
3. **`PYTHONUNBUFFERED=1`** —— 否则流式输出会被攒到最后一次性吐出，
   审查过程看起来像卡死了。

### 9.3 运行时精简

完整运行时 164.9 MB，其中 90% 是 `site-packages` 里的 numpy / pandas / Pillow / lxml。
本 Agent 是纯标准库的，一个都不需要：

```powershell
robocopy <源> runtime /E /XD site-packages test tests idlelib tkinter ... /XF *.pyc *.pyo
```

结果：**164.9 MB → 34.2 MB**。之后跑一次冒烟测试确认解释器还能用：

```python
import json, re, pathlib, urllib.request, ssl, unicodedata, sys
```

只删第三方包、不删标准库，是这里唯一的安全线 —— `Lib\encodings`、`DLLs\`（`_socket`、
`_ssl`、`select`、`unicodedata` 都是扩展模块）一个都不能少。

CPython 的许可证会随包复制为 `runtime\PYTHON-LICENSE.txt`。

---

## 10. 错误处理体系

`codeagent/errors.py` 定义了一棵树，分层的意义在于**决定怎么处理**：

```
AgentError
├── ConfigError            → 启动前拦住，给可操作提示
├── LLMError
│   ├── LLMHTTPError       → 带 status_code；retryable 决定是否重试
│   ├── LLMTimeoutError    → retryable = True
│   ├── LLMConnectionError → retryable = True
│   └── LLMResponseError   → 协议异常，不重试
└── ToolError              → 不中断循环，转成 ok=false 回灌给模型
    ├── ToolNotFound / ToolArgumentError / ToolExecutionError
    ├── ToolPermissionError
    └── WorkspaceViolation
```

**核心分野**：`LLMError` 是**运维问题**（网络、配额、密钥），重试或报错给用户；
`ToolError` 是**任务问题**（参数错、路径错），一律回灌给模型让它自己修正。

一个兜底细节：钩子（渲染回调）抛异常时 `_safe()` 会静默吞掉 ——
绝不允许"界面渲染出错"导致"审查停止"。

---

## 11. 边界情况清单

按评审维度「功能完整性 → 边界情况处理」整理，全部有对应测试：

| 场景 | 行为 | 测试 |
|------|------|------|
| API Key 无效（401） | 判定失败，打印服务端原文与排查建议 | `test_401_is_fail` |
| 触发限流（429） | 判定**警告**而非失败，仍保存 | `test_429_is_warn_not_fail` |
| 网络不通 | 判定警告，密钥保存但标记未验证 | `test_network_error_is_warn_not_fail` |
| 校验过程自身抛异常 | 兜底为警告，不让程序崩 | `test_unexpected_exception_is_absorbed` |
| `config.json` 损坏 | 按未绑定处理并提示，不崩溃 | `test_corrupt_file_is_survivable` |
| 配置目录不可写 | 返回失败与原因，不抛异常 | `test_save_reports_failure_when_no_candidate_writable` |
| 改过 model/url | 校验结论自动失效 | `test_model_and_url_commands_persist` |
| 审查路径不存在 | 明确报错，不硬编报告 | `test_review_missing_path` |
| 未绑定就审查 | 提示先绑定 | `test_review_without_binding_is_refused` |
| 审查单个文件 | 沙箱根取其父目录 | `test_workspace_is_derived_from_target_file` |
| 切换到另一个目录审查 | 重建 Agent 并重置上下文 | `test_switching_workspace_resets_context` |
| 只想审查，不想被改代码 | 写工具根本没注册 | `test_write_tool_is_not_reachable_at_all` |
| `NO_COLOR` 环境变量 | 尊重约定，关闭着色 | `test_no_color_env_is_respected` |
| 中英混排对齐 | 按显示宽度而非字符数补齐 | `test_pad_aligns_mixed_width` |
| 模型返回非法 JSON 参数 | 生成 tool 消息说明错误，循环继续 | `test_parse_error_still_produces_tool_message` |
| 模型重复调用同一工具 | 第 3 次拦截并注入纠偏提示 | `test_duplicate_call_is_blocked_after_threshold` |
| 用完步数上限 | 强制收尾，保证有输出 | `test_step_limit_forces_final_answer` |
| 路径逃逸出工作目录 | `WorkspaceViolation`，附解决建议 | `test_parent_escape_is_blocked` |
| 上下文超预算 | 按原子块裁剪，协议仍合法 | `test_keeps_protocol_valid` |
| 流式中途断线 | 不降级重放，避免重复输出 | `test_no_fallback_after_partial_output` |

---

## 12. 测试策略

208 个用例，六层：

| 层次 | 文件 | 关注点 |
|------|------|--------|
| 基础设施 | `test_config.py` | `.env` 解析、来源优先级、校验、脱敏 |
| 协议层 | `test_llm.py` | 响应解析、重试、SSE 分片合并、降级 |
| 数据结构 | `test_memory.py` | 原子块裁剪、协议完整性断言 |
| 工具层 | `test_tools.py` | 沙箱、参数校验、逐个工具的边界 |
| 编排层 | `test_agent.py` | 完整循环、自纠错、防护栏、钩子 |
| **审查模式** | `test_review.py` | 绑定存储、密钥校验分类、审查 Prompt、只读工具集、命令循环、Markdown 渲染 |

三个关键手法：

1. **`FakeTransport` 注入** —— 网络层被替换，重试次数、退避时长、降级行为、
   HTTP 状态码分类都能精确断言，测试完全离线且确定性。
2. **`ScriptedLLM` 编排模型行为** —— 真实模型很难稳定复现"返回非法 JSON"或
   "陷入重复调用"，而这两条恰恰最需要覆盖。
3. **`assert_well_formed()` 协议断言器**（在 `test_memory.py`）——
   在每一层都校验消息序列满足"tool 消息必有归属、tool_calls 必有响应"。

---

## 13. 已知限制与改进方向

诚实地列出没做和做得不够好的地方：

| 限制 | 影响 | 改进方向 |
|------|------|----------|
| 审查结果可能有误报漏报 | 需要人工复核 | Prompt 已用多层约束压制（强制行号、强制触发条件、允许说没问题），但无法根除 |
| 明文存储 API Key | 文件被读到即密钥泄露 | 接入 Windows 凭据管理器（DPAPI） |
| 代码会被发送到模型服务商 | 敏感代码外泄风险 | README 已明确警示；可改为本地模型（Ollama） |
| 工具结果靠滑动窗口 | 审查超大仓库时会丢早期上下文 | 引入向量检索做长期记忆 |
| 只支持 OpenAI 兼容协议 | 用不了 Anthropic / Gemini 原生协议 | 抽一层 Provider 适配 |
| 启动器依赖 Windows/csc | 无法跨平台构建 | 提供 PyInstaller 与 shell 启动脚本作为替代 |
| 单会话单线程 | 不能并发审查多个项目 | `CodeAgent` 无全局状态，加会话锁即可多路复用 |
| 无自动修复 | 只报问题不修 | 已有 `edit_file` 实现（在助手模式里），接一个"确认后修复"流程即可 |
| 内置运行时约 34 MB | 整包偏大 | 用 Windows embeddable 包可压到 ~15 MB（需要外网下载） |

---

## 14. 可扩展性小结

| 想做的事 | 需要改的地方 | 不用改的地方 |
|---------|-------------|-------------|
| 加一个审查工具（如查 TODO） | 新建 `Tool` 子类 + 加进 `REVIEW_TOOL_CLASSES` | `agent.py`、`llm.py`、`review_cli.py` |
| 换一套审查标准（如只查安全） | 改 `REVIEW_SYSTEM_PROMPT` 的维度清单 | 全部代码 |
| 换模型服务商 | `url` / `model` 命令，或改 `config.json` | 全部代码 |
| 加一个新命令（如 `export json`） | 在 `ReviewSession.handle` 里加一个分支 | `agent.py`、`tools/`、`llm.py` |
| 换成 GUI / Web | 写一个新前端，复用 `AgentHooks` | `agent.py`、`tools/`、`llm.py` |

依赖倒置带来的回报，集中体现在这四个"不用改的地方"上。
