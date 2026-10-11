# CodeReview —— 代码错误审查 Agent

> 一个**双击即用**的代码审查工具：给它一个文件或目录，它自己读代码、交叉验证调用关系，
> 然后按严重程度列出**可能存在的错误**。
>
> 用 **LLM 原生 API**（OpenAI 兼容协议）+ **纯标准库** 实现。
> 免安装整包约 **36 MB**，拷到任何 Windows 上双击就能跑，**目标机不需要装 Python**。

```
审查 ▸ review src

审查目标：src
工作目录：D:\my-project\src

▸ 第 1/25 步
  ▶ list_dir(path=.)
    ✔ 18 个条目 (0.00s)
▸ 第 2/25 步
  ▶ search_code(pattern=def\s+\w+, glob=*.py)
    ✔ 23 处命中 / 4 个文件 (0.00s)
▸ 第 3/25 步
  ▶ read_file(path=parser.py)
    ✔ parser.py 第 1-186 行 / 共 186 行 (0.01s)
  …

审查结论
这份代码整体可用，但 parse_range 在 end < start 时会静默返回空列表……

问题清单
🔴 严重
1. parse_range 在 end < start 时返回空列表而非报错
- 位置：`parser.py:47`
- 问题：`range(start, end)` 当 end < start 时为空，循环体一次都不执行……
```

---

## 目录

- [快速开始](#快速开始)
- [绑定 API Key](#绑定-api-key)
- [命令一览](#命令一览)
- [它是怎么工作的](#它是怎么工作的)
- [交付物构成](#交付物构成)
- [从源码构建](#从源码构建)
- [安全说明](#安全说明)
- [运行测试](#运行测试)
- [常见问题](#常见问题)
- [设计文档](#设计文档)

---

## 快速开始

### 方式一：直接用（推荐）

1. 把整个 `code-agent` 文件夹拷到任意位置
2. **双击 `CodeReview.exe`**
3. 输入 `api`，按提示粘贴 API Key（会立刻联网校验并告诉你成功还是失败）
4. 输入 `review`，按提示输入要审查的路径；或直接 `review D:\my-project\src`

也可以从命令行用（便于脚本化）：

```bat
CodeReview.exe review src                 审查 src 目录
CodeReview.exe review src\parser.py       审查单个文件
CodeReview.exe help                       打印指令说明后退出
CodeReview.exe api sk-xxxxxxxx            直接绑定并校验
CodeReview.exe --mock review src          离线演示（不调用真实模型）
```

### 方式二：从源码跑

需要本机有 Python 3.10+：

```bat
python main.py
python main.py review src
python main.py --mock
```

### 运行环境

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11（其余平台可从源码运行） |
| 运行整包 | 无额外要求，`runtime\` 已自带 Python 3.12 |
| 从源码运行 | Python ≥ 3.10 |
| 第三方依赖 | **无**（HTTP 用 `urllib`，测试用 `unittest`） |
| 网络 | 需要能访问所配置的模型端点 |

---

## 绑定 API Key

在提示符下输入 `api`，程序会提示你粘贴密钥：

```
审查 ▸ api
请输入 API Key（输入时不显示，直接回车取消）: ************

正在校验… （https://api.deepseek.com/v1 / deepseek-chat）
✔ 绑定成功：校验通过，模型 deepseek-chat 可正常调用
  耗时 0.42s
  已保存到 D:\Tools\code-agent\config.json
  注意：密钥以明文保存在该文件中，请勿提交到版本库或分享。
```

### 校验是怎么做的

绑定不是"存下来就算完"，而是**真的发一次请求**（一个字符的提示词 + `max_tokens=8`，
成本可以忽略），所以你能立刻知道结果。结果分三档而不是简单的成功/失败：

| 档位 | 触发条件 | 程序行为 |
|------|---------|---------|
| **成功** ✔ | HTTP 2xx，模型正常返回 | 标记为"已校验通过" |
| **警告** ⚠ | 限流 429、服务端 5xx、网络不通 | **仍然保存**，标记"未通过校验"——这些不代表你密钥填错了 |
| **失败** ✖ | 401 密钥无效 / 402 余额不足 / 403 无权限 / 404 端点或模型不存在 | 保存并标记失败，同时打印服务端原文与排查提示 |

失败时会直接把服务端返回给你看，并给出下一步，例如：

```
✖ 绑定失败：API Key 无效或已被停用
  服务端返回：{"error":{"message":"Authentication Fails, Your api key: ****0000 is invalid", ...}}
  提示：可先用 url / model 指令确认端点和模型名，再重新执行 api。
```

### 相关命令

| 命令 | 作用 |
|------|------|
| `api` | 提示输入并校验 |
| `api <key>` | 直接用给定的密钥绑定并校验 |
| `api show` | 查看绑定状态（密钥脱敏为 `sk-abc********wxyz`） |
| `api clear` | 解除绑定（删除 `config.json`） |
| `url <地址>` | 换端点，例如 `url https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `model <名称>` | 换模型，例如 `model qwen-plus` |

> 改过 `url` / `model` 后校验结论会自动失效，建议重新执行一次 `api`。

### 已适配的服务商

因为走的是标准 `/chat/completions` + function calling 协议，任何 OpenAI 兼容端点都能用：

| 服务商 | `url` | 示例模型 |
|--------|-------|---------|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Kimi | `https://api.moonshot.cn/v1` | `kimi-k2-0905-preview` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5-coder` |

---

## 命令一览

输入 `help` 随时查看：

| 命令 | 作用 |
|------|------|
| `help` / `?` | 显示帮助 |
| `api` | 绑定 API Key（提示输入 + 联网校验） |
| `api show` / `api clear` | 查看 / 解除绑定 |
| `model <名称>` / `url <地址>` | 设置模型 / 端点 |
| `review` | 审查（提示输入路径） |
| `review <路径>` | 直接审查指定文件或目录 |
| `save [路径]` | 把上一条审查报告保存为 Markdown |
| `config` | 显示完整配置与配置文件位置 |
| `exit` / `quit` / `q` | 退出 |

**另外两种输入方式**：

- 直接输入一个已存在的路径 → 等价于 `review <路径>`
- 直接输入一句自然语言 → 按这句话的要求审查，例如
  `检查 codeagent 目录下所有 Python 文件的空值处理问题`

---

## 它是怎么工作的

### Agent 循环

不是"把代码贴给模型让它评价"，而是真正的 Agent 循环：

```
用户给出目标
    │
    ▼
┌──────────────────────────────────────────┐
│ 1. 组装上下文（system prompt + 历史）      │
│ 2. 调用模型                               │
│ 3. 模型要求调用工具？                      │
│      是 → 执行工具 → 结果回灌 → 回到 1     │
│      否 → 输出最终答案                     │
└──────────────────────────────────────────┘
```

模型会自己决定：先 `list_dir` 看结构，用 `search_code` 定位可疑点，
再 `read_file` 精读。怀疑某个函数调用有问题时，它会去读被调函数的定义来**交叉验证**——
这正是"审查"和"看一眼"的区别。

### 三个只读工具

审查只需要"看"，所以工具集是刻意收窄的：

| 工具 | 参数 | 作用 |
|------|------|------|
| `list_dir` | `path? recursive? max_depth?` | 列目录，自动跳过 `.git`/`node_modules`/`__pycache__` |
| `read_file` | `path start_line? end_line? max_lines?` | 读文本文件，带行号；支持分段 |
| `search_code` | `pattern glob? path? ignore_case?` | 正则检索，返回 `文件:行号: 内容` |

**没有** `write_file` / `edit_file` / `run_command` / `run_python`。
它们不是被禁用了，而是**根本没注册**——模型看不到、也调不动，
所以审查过程绝不会改动你的代码，也不需要你担心中途执行了什么命令。

### 审查规程

`codeagent/review_prompt.py` 里是一份可执行的审查规程，而不是泛泛的角色设定：

- **第一原则：证据来自工具，不来自记忆。** 不许凭文件名猜内容，不许凭函数名猜签名，
  查不到就写"未能确认"，**绝不编造问题**。
- **八个审查维度**（按优先级）：正确性缺陷 → 空值与类型 → 异常处理 → 资源与生命周期 →
  并发与状态 → 安全 → 可复现性 → 会导致错误的性能问题。
- **明确排除**：命名、缩进、注释多少、"建议用列表推导式"这类风格偏好。
  它们只会把真正的缺陷淹没。
- **强制输出格式**：按 🔴 严重 / 🟠 中等 / 🟡 轻微 分组，
  每条必须给出 `文件:行号` + 问题 + 影响 + 修复建议，最后附"未确认项"。
- **允许说没问题**：明确写了"没有发现问题就如实说，为显得有产出而编造问题是最严重的失败"。

---

## 交付物构成

```
code-agent\
├── CodeReview.exe          ★ 双击这个。7.5 KB 的启动器（C# 编译）
├── main.py                 Python 入口
├── config.json             绑定信息（含明文密钥，首次绑定后生成）
│
├── codeagent\              Agent 内核（约 3.5 MB → 实际 0.5 MB，纯源码）
│   ├── review_cli.py       ★ 审查模式前端：命令循环 / 渲染 / 报告保存
│   ├── review_prompt.py    ★ 审查规程（System Prompt）
│   ├── agent.py            ★ ReAct 主循环
│   ├── llm.py              OpenAI 兼容客户端（流式 / 重试 / 降级）
│   ├── memory.py           上下文记忆与原子块裁剪
│   ├── verify.py           ★ API Key 联网校验与错误分类
│   ├── store.py            ★ config.json 读写（含只读目录降级）
│   ├── ui.py               着色 / 输入 / 宽度对齐
│   ├── cli.py              完整助手模式（保留，非默认）
│   ├── prompts.py / config.py / errors.py / mock_llm.py
│   └── tools\              Tool 抽象 / 注册表 / 沙箱 / 7 个工具实现
│
├── runtime\                自带精简 Python 3.12（34.2 MB，660 个文件）
├── launcher\CodeReview.cs  启动器源码
├── build.ps1 / build.cmd   一键构建
├── tests\                  208 个用例
├── README.md / DESIGN.md
└── .gitignore / .env.example / requirements.txt
```

> 完整运行时是 164.9 MB，其中 90% 是 numpy / pandas / Pillow 这类第三方包——
> 本 Agent 是纯标准库的，一个都不需要，所以剔掉 `site-packages` 后降到 34.2 MB。

---

## 从源码构建

```bat
build.cmd
```

脚本做两件事，**不下载任何东西**：

1. 用 Windows 自带的 `csc.exe`（.NET Framework 编译器）把 `launcher\CodeReview.cs`
   编译成 `CodeReview.exe`；
2. 从本机已有的 Python 安装里复制出一个精简运行时到 `runtime\`，并做一次冒烟测试。

常用参数：

```bat
build.cmd -SkipRuntime                       只重新编译 exe
build.cmd -PythonSource "C:\Python312"       指定运行时来源
build.cmd -Force                             强制重建
```

> PowerShell 默认禁止运行 `.ps1`，所以请用 `build.cmd`（它内部带了
> `-ExecutionPolicy Bypass`，只影响本次调用，不改动系统设置）。
> 若坚持直接跑脚本：`powershell -ExecutionPolicy Bypass -File build.ps1`

---

## 安全说明

### 这个工具做了什么

1. **只读工具集** —— 写文件、执行命令的工具根本没有注册，模型无法触及。
2. **双保险的只读上下文** —— 即使有人往注册表里塞了写工具，`ToolContext.allow_write=False`
   也会在工具层拒绝。
3. **路径围栏** —— 所有文件路径都经过解析与校验，必须落在审查目标的目录树内，
   符号链接也会被跟随后再校验。
4. **配置不落版本库** —— `config.json` 已在 `.gitignore` 中忽略。

### 你必须知道的

- ⚠️ **`config.json` 里是明文 API Key。** 这是刻意的取舍：要加密就得引入密钥管理，
  反而更不透明。请勿把它提交到版本库或分享给他人。
  程序目录不可写时会自动降级到 `%USERPROFILE%\.codereview\config.json`。
- ⚠️ **审查结果由大模型生成，可能存在误报与漏报。** System prompt 里已经用大量约束
  压制幻觉（要求给出 `文件:行号`、要求说明"什么情况下会出错"、允许说没问题），
  但它仍是**辅助工具**，不能替代人工评审与测试。
- 审查目标的目录树会被发送给所配置的模型服务商。**审查敏感代码前请确认这一点。**
- 每个 API Key 绑定的是一次极小请求用于校验（约几个 token 的成本）。

---

## 运行测试

```bat
python -m unittest discover -s tests -t .
```

预期输出：

```
Ran 208 tests in 13.1s

OK
```

测试**完全离线**：LLM 调用通过注入 `FakeTransport` 覆盖，模型行为通过 `ScriptedLLM`
精确编排，因此"模型返回非法 JSON""陷入重复调用""API 返回 401/429/5xx 分别怎么归类"
这些难以复现的分支都能被稳定测试。

---

## 常见问题

| 现象 | 原因与解决 |
|------|-----------|
| 双击 exe 一闪而过 | 程序以非零退出码结束时会自动暂停并显示原因；若没看到，说明 `runtime\` 缺失，请重新解压整包 |
| `启动失败：找不到程序文件 main.py` | 只拷了 exe。整个文件夹一起拷 |
| `启动失败：找不到可用的 Python 解释器` | `runtime\` 不完整；或自己装了 Python 但没加到 PATH |
| 绑定提示"无法完成校验（网络问题）" | 密钥已保存但未验证。检查代理/网络后重新执行 `api` |
| `HTTP 404` | `url` 少写 `/v1`，或 `model` 名字不对。用 `config` 确认 |
| `HTTP 400` 且提示 tools | 该模型不支持 function calling，换一个 |
| 审查到一半停了 | 达到 `max_steps`（默认 25），会强制基于已有信息收尾。可在 `config.json` 里调大 |
| 中文显示成方块 | 换个等宽字体；🔴 之类的 emoji 在旧终端可能不显示，但对应的"严重"文字仍在 |
| 报告太长看不完 | `save` 存成 Markdown 文件慢慢看 |

---

## 设计文档

实现细节、算法说明与设计权衡见 **[DESIGN.md](DESIGN.md)**，包含：

- Agent 循环的完整状态机与六道防护栏
- 审查规程的设计：为什么"禁止编造"要写进 prompt、八个维度的来源
- 上下文裁剪的"原子块"算法（为什么不能简单丢掉最旧的消息）
- LLM 客户端的重试策略、流式分片合并与降级
- 密钥校验的三档分类，以及为什么限流不能判成失败
- 免安装整包的构建链路：csc 编译启动器 + 精简 Python 运行时
- 边界情况清单与测试策略
- 已知限制与改进方向
