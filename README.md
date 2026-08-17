# DS娘 AI 智能助手

一款桌宠形态的 AI 工作助手：通过任务管理器 / 前台窗口 / 浏览器历史感知“你正在做什么、过去做过什么”，
用**一个目录文件 + 多个目的文件**实现可读、可编辑的长期记忆，并接入 DeepSeek 帮助你继续手头的工作。

---

## 1. 核心设计

### 1.1 双文件记忆系统

| 文件 | 数量 | 内容 | 大模型如何使用 |
|---|---|---|---|
| `memory/目录.md` | 全局唯一 | 每条记忆的**大致主题、发生时间、权重、类型、标签、摘要、目的文件地址** | 每次聊天只读它 |
| `memory/目的/<ID>-<主题>.md` | 每条记忆一个 | 那次对话的完整内容，或那次工作的具体地址/窗口/进程/浏览记录 | 只有聊天涉及相关主题时，按目录中的地址打开 |

目录文件格式为“人类可读的 Markdown + 每条一个 YAML 元数据块”，可直接用编辑器打开修改；
目的文件同样是带元数据的 Markdown。大模型绝不会直接读取目的文件目录，只认目录文件里出现过的路径。

### 1.2 两段式记忆检索

1. 用户发消息后，引擎把**目录文件摘要**放进上下文；
2. 先让 DeepSeek 当“检索器”，从目录中选择需要打开的 `id`（不相关则返回空）；
3. 仅打开被选中的目的文件，拼接上下文后正式回答。

### 1.3 独立重要性判定程序

`assistant/judge/importance.py` 可单独运行（不启动 GUI、不依赖数据库）：

```bash
python -m assistant.judge.importance judge "明天要和客户签合同" --source chat --duration 1800
python -m assistant.judge.importance recalc --commit
```

判定因素：

- 来源类型（用户显式说明 > 配置修改 > 聊天 > 工作 > 浏览 > 任务管理器快照）
- 关键词信号（重要、紧急、deadline、bug、决定、TODO……）
- 持续时长 / 消息规模
- 同主题在目录中的出现频率（长期主线加成）
- 距现在的时间衰减（时间相隔越久权重越低）
- 可选：`judge.use_ai=true` 时调用 DeepSeek 给语义重要性分

只有分数达到 `judge.threshold` 的事件才写入目录/目的文件；**配置修改是唯一强制写入的类型**。

### 1.4 活动感知

| 渠道 | 默认 | 说明 |
|---|---|---|
| 前台窗口标题 + 进程 | 开 | Windows 用 ctypes，Linux 尝试 xdotool |
| 任务管理器式进程快照 | 开 | 只保留 CPU/内存 Top N 摘要，不读取进程内存内容 |
| Chrome/Edge 历史 | 关 | 默认关闭，需在设置中主动开启；只读最近 N 天并去重 |

会话结束/切换/超时后，活动监视器把会话交给判定器，足够重要才写入双文件记忆。
聊天时监视器只提供“实时活动上下文”给大模型，不会把未经判定的垃圾活动直接写进长期记忆。

### 1.5 本地文件工具与权限控制

助手通过 DeepSeek Function Calling 直接操作本地文件：

| 工具 | 说明 |
|---|---|
|  | 读取工作区内文本文件 |
|  | 创建/覆盖 UTF-8 文本文件 |
|  | 追加文本 |
|  | 精确文本替换 |
|  | 列出目录 |
|  | 删除文件（默认关闭） |

权限控制：

- 默认仅允许访问 （默认 ，即程序所在目录）；
- 读 / 写 / 改 / 删 / 列目录分别有独立开关；
- 写、改、删操作默认**逐次弹窗确认**；可开启  自动批准；
- 路径经过 ，阻止  与符号链接逃逸；
- 超出  的文件拒绝读写。

---

## 2. 已实现功能

- DS娘桌宠：无边框透明、置顶、鼠标随意拖动、轻微浮动动画、连续点击打开聊天、右键菜单
- 用户可给助手命名；默认语气为**软糯可爱温柔可靠**，可自行改写整段人设提示词
- 聊天界面融入 DS 蓝 + 软糯粉元素，保持简洁
- 设置面板：助手、DeepSeek、记忆判定、文件工具权限、活动监视、桌宠六类参数，全部经过范围校验
- 聊天界面显示助手的思考过程，可折叠/展开；工具调用与执行结果也会显示在思考面板
- 聊天窗口顶部提供“退出程序”按钮
- 助手可通过文件工具直接读写本地文件，权限可限制、写操作可逐次批准
- 每一次设置修改都强制写入目录文件与目的文件
- 命令行模式：`python run.py chat`（无 GUI 也能用）
- 目录文件与目的文件均可手动编辑；单条记忆损坏不影响其他条目

---

## 3. 快速开始

```bash
# 1. 安装依赖（建议 Python 3.10+）
pip install -r requirements.txt

# 2. 配置 DeepSeek API Key（二选一）
#    A. 环境变量
export DEEPSEEK_API_KEY="sk-..."
#    B. 在设置界面填写，或直接编辑 config.json 的 deepseek.api_key

# 3. 启动桌宠与聊天界面
#    方式一：直接双击“启动DS娘.bat”（会显示一个极简启动窗口）
#    方式二：直接双击“启动DS娘.vbs”（完全无窗口，推荐）
python run.py

# 4. 无 GUI 命令行聊天
python run.py chat
```

首次运行会自动生成：

```text
config.json         用户可调参数（带校验）
memory/目录.md       唯一目录文件
memory/目的/         目的文件目录
data/                运行数据
```

## 4. 参数表（节选）

全部参数见 `config.json`。常用参数：

| 参数 | 默认 | 范围 |
|---|---|---|
| `assistant_name` | DS娘 | 文本 |
| `persona` | 软糯可爱温柔可靠提示词 | 文本 |
| `deepseek.model` | deepseek-chat | deepseek-chat / deepseek-reasoner |
| `deepseek.temperature` | 0.7 | 0 ~ 2 |
| `judge.threshold` | 0.45 | 0 ~ 1 |
| `memory.decay_half_life_days` | 30 | 0.5 ~ 3650 |
| `activity_monitor.enabled` | true | 布尔 |
| `activity_monitor.browser_history_enabled` | false | 布尔 |
| `pet.size` | 170 | 48 ~ 600 |
| `pet.click_count_to_open` | 2 | 1 ~ 5 |

## 5. 项目结构

```text
assistant/
  config.py              参数配置与范围校验
  app.py                 装配 + 工作会话记忆写入 + CLI
  desktop.py             GUI 启动
  memory/manager.py      目录文件/目的文件读写（路径安全校验）
  judge/importance.py    独立重要性判定与权重重算
  brain/deepseek_harness.py  DeepSeek / OpenAI 兼容接口封装
  brain/chat_engine.py   两段式记忆检索 + 工具调用循环 + 思考过程事件
  tools/file_tools.py    本地文件工具与权限控制
  monitor/               窗口探针、浏览器历史、活动监视器
  ui/                    DS娘桌宠、聊天窗口、设置界面
memory/目录.md            记忆目录（自动生成）
memory/目的/              目的文件
```

## 6. 隐私说明

- 活动监视只在本机内存中聚合，只有达到重要性阈值的摘要才写入本机 Markdown 文件；
- 浏览器历史默认关闭；进程快照不读取进程内存、不截屏；
- API 调用只发送你当前对话与已被判定为相关的记忆上下文；
- 文件工具默认锁在工作区内，写操作需用户批准；不需要时可整体关闭；
- 不需要监视时，可在设置中一键关闭。

## 7. 测试

核心逻辑不依赖 GUI，可直接运行：

```bash
python -m unittest discover -s tests -v
```
