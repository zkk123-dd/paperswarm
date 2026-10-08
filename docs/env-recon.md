# 环境勘察报告：Linux 沙箱与本地工具链

> 全部结论均为 2026-09-29 在本机（Windows，Git Bash + 隔离 Python 3.13.12）实测所得。
> 每条结论都附实测证据，未核实项一律标 `[待核实]`。

---

## 1. 结论摘要

| 项 | 状态 | 对项目的影响 |
|---|---|---|
| WSL（路线 A 的核心） | **被安全策略硬阻断，我无法推进** | 复现实验无法在 Linux 沙箱内执行 |
| Ollama 本机推理 | **可用，且函数调用验证通过** | **API key 阻塞解除**，链路可真实跑通 |
| LaTeX 编译链 | 缺失 | PDF 交付物暂时无法产出 |
| Python 科学栈 | 基本齐备（已补齐缺口） | 证据层、遥测、Agent 循环可运行 |
| `pdfinfo` | 缺失，但 PyMuPDF 可替代 | 论文页数/体积校验不受阻 |
| git / GitHub | git 2.55 可用，GitHub 连接器已连接 | 代码托管与 PR 通路正常 |

---

## 2. WSL：硬阻断（这是路线 A 的死结）

### 实测证据

本机存在 `C:\Windows\System32\wsl.exe`，但任何调用都被拦：

```
PROGRAM BLOCKED BY SECURITY POLICY - The sandbox prevented a program on the
configured Program Blacklist from starting:
  - wsl.exe (C:\Windows\System32\wsl.exe)
This block cannot be approved or bypassed from the current command.
Do NOT retry this program, launch it through another shell or script,
or attempt an equivalent workaround.
```

从 Bash 调用 `cmd.exe` 同样被拒：

```
Command blocked for security: Invoking cmd.exe from Bash bypasses all command validation
```

### 这意味着什么

1. **我不能安装 WSL，也不能查询它的状态**，连 `wsl -l -v` 都跑不了。
2. 策略明确禁止绕过（换 shell、写脚本、找等价入口都不行）。**所以我不会去试**。
3. 关键推论：**即使你手动装好 WSL2，我依然无法调用 `wsl.exe`**——它在黑名单里，
   装好也调不动。要让我接管后续步骤，**解除黑名单是前提，不是可选项**。

### 解除方式（需要你自己操作）

Security Center → Command Security → Program Blacklist → 把 `wsl.exe` 移除。

### 解除后我可以接着做的事

```bash
wsl --install -d Ubuntu-24.04 --no-launch
wsl -d Ubuntu-24.04 -- bash -lc "sudo apt-get update && sudo apt-get install -y python3.12-venv build-essential"
# 然后在 WSL 内装 jiuwenswarm + jiuwenbox
```

Linux 侧还需要确认磁盘落点：`C:` 剩 27G、`D:` 剩 60G、`E:` 剩 201G。
WSL 发行版默认落在 `C:`，**建议迁移到 `E:`**（201G 余量，且项目已在 E:）。
`[待核实: WSL 发行版迁移命令 `wsl --export` / `wsl --import` 在指定版本上的具体参数]`

---

## 3. 替代路线（不需要解除黑名单）

如果不想动安全策略，有三条替代路径。它们不是等价替代，各自的适用边界写清楚了。

### 路线 A2：远程 Linux 主机（最接近原目标）

一台云端 Linux（含 GPU 更好），我用 SSH 驱动。能完整支持 `jiuwenbox`，
算力上限也最高。需要你提供主机与凭证（走环境变量或密钥文件，别贴对话里）。

### 路线 A3：GitHub Actions 的 ubuntu runner（零安装，今天就能用）

本项目 GitHub 连接器已连接，可以走 CI 跑实验：

- **优点**：真 Linux、真隔离、免费额度（公开仓库）、每次运行有完整日志与耗时，
  天然满足"资源报告可追溯"；实验结果与 commit 一一对应，可复现性强。
- **边界**：`ubuntu-latest` 是 CPU 机器（约 4 vCPU / 16GB 内存量级，`[待核实: 具体规格与分钟配额以 GitHub 官方文档为准]`），
  **没有 GPU**。
- 与本项目设计相容：FARS 自承无法处理算力密集任务，我们的 `scout` 本来就硬性筛除
  大算力论文。**小规模 CPU 复现恰好是我们选定的实验区间**，不是妥协。
- 用不了 `jiuwenbox`（那是本地沙箱），但 CI runner 本身就是隔离环境。

**我的建议：A3 先跑通，A1/A2 并行准备。** 理由是它不需要任何安装、不需要凭证、
今天就能产出真实的实验与资源数据；等 Linux 沙箱到位后再把执行后端从 CI 切回来，
而证据台账（SHA-256 绑定）那层不受影响——这正是把证据层做成零框架依赖的收益。

---

## 4. 本机已有的能力（已实测）

### 4.1 Ollama：**API key 阻塞已解除**

| 项 | 实测值 |
|---|---|
| 版本 | 0.34.4 |
| 端点 | `http://127.0.0.1:11434/v1`（OpenAI 兼容，服务在运行） |
| 模型 | `qwen2.5:7b-instruct-q4_K_M`（7.6B，Q4_K_M，上下文 32768） |
| 能力声明 | `capabilities: ["completion", "tools"]` |
| **函数调用实测** | 返回 `finish_reason=tool_calls`，工具名与 JSON 参数均正确 |
| 生成吞吐 | **30.6 tok/s**（169 completion tokens / 5.53s） |
| 边际成本 | **0 元**（本机推理，非"未定价"） |

实测的函数调用响应：

```json
{
  "finish_reason": "tool_calls",
  "tool_calls": [{
    "id": "call_bp4xijon",
    "function": {
      "name": "register_artifact",
      "arguments": "{\"label\":\"results.json\",\"path\":\"repro-pack/results.json\"}"
    }
  }]
}
```

**结论**：Agent 循环的命门（函数调用）在本机可用，且成本为零。
这对"资源报告"和"成本实验"是加分项——可以放开跑而不担心账单。

**但它不能替代前沿模型写 ICLR 级英文论文**，这一点必须诚实标注：

| 用途 | 7B 本地模型 | 判断 |
|---|---|---|
| 跑通链路、产生真实 token/时延数据 | 足够 | 已验证 |
| 工具调用与结构化输出 | 足够 | 已验证 |
| 写 ICLR 级英文正文 | **不足** | 需要更强模型，且这是**假设**，待实测对比 |

因此模型策略应为**分档路由**：链路验证与批量机械步骤用本地模型，
正文写作与对抗审稿用强模型。`configs/model.yaml` 已按此结构写好。

### 4.2 缺失的工具链

| 工具 | 状态 | 处理方案 |
|---|---|---|
| pdflatex / xelatex / lualatex / latexmk / tectonic | **全缺** | 必须安装其一；ICLR 模板需要真 LaTeX 引擎 |
| pandoc | 缺 | 本项目不用它（我们走 `\result{}` 宏 + LaTeX 直出） |
| pdfinfo（poppler） | 缺 | **PyMuPDF 已装，可完整替代**页数与体积校验 |

LaTeX 引擎选型建议：**tectonic**（单文件二进制、按需拉取宏包，体积远小于 TeX Live）。
`[待核实: tectonic 在 Windows 上的发布方式、版本号与许可证，以官方发布页为准]`
备选 MiKTeX / TeX Live。这一项下一步要落地——没有它就没有 PDF 交付物。

### 4.3 Python 环境

隔离环境 `C:\Users\周凯\.workbuddy\binaries\python\envs\default`，本轮新增安装：

```
pydantic==2.11.7  jsonschema==4.25.1  PyYAML==6.0.2  tenacity==9.1.2  pypdf==6.1.1
```

已有：`numpy` / `matplotlib` / `pandas` / `requests` / `PyMuPDF(fitz)`。

---

## 5. Shell 使用的两个坑（避免重复踩）

1. **Bash 需要显式设 PATH**，否则 `dirname: command not found`：
   ```bash
   export PATH="/usr/bin:/bin:$PATH"
   ```
2. **PowerShell 工具在本机不返回任何 stdout**，实测无效。需要 Windows 侧能力时
   走 Bash + `/c/Windows/System32` 下的可执行文件（但 `cmd.exe`、`wsl.exe` 被策略拦）。

---

## 6. 资源占用与性能影响（实测）

本节回答"会不会吃内存、会不会拖慢机器"。全部为 2026-09-29 在本机实测值，非估算。

### 6.1 本机容量与当前水位

| 资源 | 容量 | 当前占用 | 余量 |
|---|---|---|---|
| 物理内存 | 15.78 GB | 11.14 GB（**70%**） | **4.64 GB** |
| GPU 显存 | 8 GB（RTX 4060 Laptop） | 1.2 GB（桌面/系统） | 6.8 GB |
| CPU | 20 逻辑核 | — | 充裕 |
| C 盘 | 201 GB | 174 GB（87%） | **27 GB ← 最大瓶颈** |
| D 盘 | 275 GB | 216 GB | 60 GB |
| E 盘 | 477 GB | 281 GB | 197 GB |

### 6.2 本地推理时的实测占用曲线

同一次生成（约 900 token），每 2 秒采样：

| 时刻 | 显存占用 | Ollama 主内存 |
|---|---|---|
| 基线（模型未加载） | 1238 MiB | 39 MB |
| 推理中（第 4 秒起稳定） | 6092 MiB | 42 MB |
| 生成结束后 5 秒 | 6092 MiB（仍在驻留） | 42 MB |

**三个结论**：

1. 模型 **4.85 GB 全部 offload 到 GPU**，主内存**只增加 3 MB**。
2. CPU 占用近似为 0——`typeperf "\Process(ollama)\% Processor Time"` 三次采样均为 `0.00%`。
3. 即：本地推理**不抢主内存、不抢 CPU**，代价是**独占约 5 GB 显存**。

8 GB 显存扣掉桌面占用（1.2 GB）后剩约 3 GB，**同时打游戏或跑 GPU 程序时会显存不足**。

### 6.3 显存可随时释放（已实测）

```bash
curl -s http://127.0.0.1:11434/api/generate \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:7b-instruct-q4_K_M","keep_alive":0}'
```

实测：显存 **6105 MiB → 1231 MiB，3 秒内释放完毕**。默认行为是空闲 5 分钟后自动卸载，
所以**不会长期霸占显存**。

### 6.4 真正的硬约束（后续动作必须遵守）

| 约束 | 数值 | 后果 | 对策 |
|---|---|---|---|
| C 盘余量 | 27 GB | WSL / Docker / TeX Live 默认全装 C 盘，会挤爆 | 一律指定到 **E 盘**；LaTeX 用 **tectonic**（单文件、体积远小于 TeX Live） |
| 主内存余量 | 4.64 GB | WSL2 默认内存上限约为总内存的 50%（≈7.9 GB），会直接触发换页、整机卡顿 | 必须写 `~/.wslconfig` 限制内存（**当前该文件不存在**） |
| Ollama 模型 | 5.9 GB（`E:\OllamaModels`） | **不占 C 盘** | 无需处理（见 §6.6） |

`[待核实: WSL2 内存上限默认值（50% 还是 80%）随版本变化，以 Microsoft 官方文档当前版本为准]`

### 6.6 已落地的处置（2026-09-29）

**① 修正一处前期错误判断：Ollama 模型本就在 E 盘**

前期记录写过「模型占 C 盘 4.7 GB，可迁移 `OLLAMA_MODELS` 到 E 盘」——**该判断是错的**，
属于未核实默认路径就下的结论。实测：

- 系统级环境变量 `OLLAMA_MODELS = E:\OllamaModels` **早已存在**（HKLM，非本次新增）
- 模型实体全部在 E 盘：`blobs` 4.4 GB，目录合计 **5.9 GB**
- `C:\Users\周凯\.ollama\` 仅 **11 KB**（配置、密钥、history），**无 `models` 子目录，无残留**

**结论：不需要任何迁移动作。** 这也说明 C 盘 174 GB 的占用与 Ollama 无关。

**② 已写入 `C:\Users\周凯\.wslconfig`**（此前该文件不存在，本次新建）

| 配置项 | 值 | 理由 |
|---|---|---|
| `[wsl2] memory` | `5GB` | 本机内存余量仅 4.64 GB；默认上限约 7.9 GB 会触发换页 |
| `[wsl2] processors` | `6` | 20 个逻辑核中划 6 个给 WSL，其余留给 Windows |
| `[wsl2] swap` / `swapFile` | `2GB` / `E:\WSL\swap.vhdx` | C 盘余量紧张，交换文件放 E 盘（`E:\WSL` 目录已创建） |
| `[experimental] autoMemoryReclaim` | `gradual` | 让 WSL 归还空闲内存，缓解「用过的内存不还给 Windows」 |
| `[experimental] sparseVhd` | `true` | 让 `ext4.vhdx` 支持稀疏，删除内容后磁盘占用才能真正下降 |

生效方式：保存后执行 `wsl --shutdown`，下次启动 WSL 时读取。

`[待核实: [experimental] 两项所需的 WSL 最低版本号，以 Microsoft 官方文档为准]`

**③ 附带发现（未处理，等 ii 决定）**：`E:\OllamaModels\ollama-set up.exe`
是 1.57 GB 的安装包残留，与模型无关，可清理。**我未擅自删除用户文件。**

### 6.5 项目自身占用

`E:\存放\agent\paper-swarm` 总计 **403 KB**（`src` 132 KB / `runs` 127 KB）。
证据层与 Agent 循环的运行时内存开销在几十 MB 量级，可忽略不计。

---

## 7. 待确认清单

| 编号 | 内容 | 核实方式 |
|---|---|---|
| Q15 | WSL 发行版迁移到 E: 的确切命令与参数 | 查 Microsoft 官方 WSL 文档 |
| Q16 | GitHub Actions ubuntu runner 的规格与免费额度 | 查 GitHub 官方定价与 runner 文档 |
| Q17 | tectonic 的 Windows 发布形式与版本 | 查 tectonic 官方发布页 |
| Q18 | 本机 7B 模型与强模型在论文写作上的质量差距 | 同一任务双模型对比实测（Phase 5 评测集） |
| Q19 | WSL2 内存上限默认值（占物理内存比例） | 查 Microsoft 官方 WSL 配置文档 |
